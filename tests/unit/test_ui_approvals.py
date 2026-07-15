"""UI approvals + nonce lifecycle + Gate routes (Phase 8, Task 3).

The safety-critical half is the ``ApprovalManager``: an ASK awaits a human decision, and a
resolution needs a single-use nonce minted only to a live, watching client (ADR-0008 §3).
These run as async units (real futures, one event loop). ``UIApprover`` parity proves "always"
persists identically to the REPL. The routes are thin wrappers, tested for auth + wiring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport

from kira.config import load_config
from kira.core.client import ToolCall
from kira.core.execution import ExecutionContext, bind_execution_context
from kira.permissions import PermissionGate, load_policy
from kira.permissions.gate import Decision
from kira.tools import Permission
from kira.ui.approver import ApprovalManager, UIApprover, make_ui_subagent_approver
from kira.ui.auth import SESSION_COOKIE, AuthManager
from kira.ui.connections import ConnectionManager
from kira.ui.server import create_app

ASK = Decision(Permission.ASK, "needs approval")
_CONTEXT = ExecutionContext(session_id=101, project_id=None)


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.delivered = asyncio.Event()

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)
        self.delivered.set()


def _cm() -> ConnectionManager:
    # A fixed clock at t=0 keeps freshly-registered connections live for the test.
    return ConnectionManager(heartbeat_seconds=15.0, clock=lambda: 0.0)


def _conn(cm: ConnectionManager):
    conn = cm.register(_FakeWS(), owner_session="test")
    cm.bind_workspace(
        conn,
        owner_session="test",
        workspace_id="w" * 24,
        context=_CONTEXT,
    )
    return conn


def _task(awaitable):
    with bind_execution_context(_CONTEXT):
        return asyncio.create_task(awaitable)


def _call(name: str = "read_file", **inp) -> ToolCall:
    return ToolCall("c1", name, inp or {"path": "notes.txt"})


async def _start(approvals: ApprovalManager, call: ToolCall, on_always=lambda: None):
    """Kick off request() as a task and let it register + broadcast; return the task."""
    task = _task(approvals.request(call, ASK, kind="turn", title=None, on_always=on_always))
    await asyncio.sleep(0)
    return task


# --- ApprovalManager: request / resolve ------------------------------------


async def test_approve_once_resolves_allow() -> None:
    cm = _cm()
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    task = await _start(approvals, _call())
    (pending,) = approvals.pending()
    await asyncio.wait_for(conn.ws.delivered.wait(), timeout=0.1)
    assert conn.ws.sent[0]["type"] == "approval"  # announced to the live client
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    ok, _ = approvals.resolve(pending.decision_id, nonce, "approve")
    assert ok
    assert await task is Permission.ALLOW
    assert approvals.pending() == []  # cleared


async def test_deny_resolves_deny() -> None:
    cm = _cm()
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    task = await _start(approvals, _call())
    (pending,) = approvals.pending()
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    approvals.resolve(pending.decision_id, nonce, "deny")
    assert await task is Permission.DENY


async def test_always_runs_on_always_and_allows() -> None:
    cm = _cm()
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    marker: list[str] = []
    task = await _start(approvals, _call(), on_always=lambda: marker.append("persisted") or "did")
    (pending,) = approvals.pending()
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    approvals.resolve(pending.decision_id, nonce, "always")
    assert await task is Permission.ALLOW
    assert marker == ["persisted"]  # the narrow-persist ran exactly once


# --- non-persistable (tainted egress, Phase 9) -----------------------------

_TAINTED = Decision(Permission.ASK, "private data was read this turn", persistable=False)


async def test_to_public_carries_persistable_flag() -> None:
    cm = _cm()
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    # A persistable ASK advertises persistable: true.
    task = await _start(approvals, _call())
    (pending,) = approvals.pending()
    assert pending.to_public()["persistable"] is True
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    approvals.resolve(pending.decision_id, nonce, "deny")
    await task
    # A tainted-egress ASK advertises persistable: false (client hides "Always allow").
    t2 = _task(
        approvals.request(
            _call("web_fetch", url="http://x"),
            _TAINTED,
            kind="turn",
            title=None,
            on_always=lambda: "should-not-run",
        )
    )
    await asyncio.sleep(0)
    (p2,) = approvals.pending()
    assert p2.to_public()["persistable"] is False
    approvals.resolve(p2.decision_id, await approvals.mint_nonce(p2.decision_id, conn), "deny")
    await t2


async def test_always_on_non_persistable_does_not_persist() -> None:
    # Structural guarantee: even if a crafted client sends "always" on a non-persistable
    # decision, on_always never runs — it degrades to approve-once (ALLOW, nothing persisted).
    cm = _cm()
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    marker: list[str] = []
    task = _task(
        approvals.request(
            _call("web_fetch", url="http://x"),
            _TAINTED,
            kind="turn",
            title=None,
            on_always=lambda: marker.append("persisted") or "did",
        )
    )
    await asyncio.sleep(0)
    (pending,) = approvals.pending()
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    approvals.resolve(pending.decision_id, nonce, "always")
    assert await task is Permission.ALLOW  # the action still proceeds once
    assert marker == []  # but nothing was persisted


# --- the nonce / replay matrix (the load-bearing safety property) ----------


@pytest.mark.parametrize("action", ["approve", "always", "deny"])
async def test_resolve_without_nonce_refused(action: str) -> None:
    cm = _cm()
    _conn(cm)
    approvals = ApprovalManager(cm)
    task = await _start(approvals, _call())
    (pending,) = approvals.pending()
    ok, msg = approvals.resolve(pending.decision_id, "", action)
    assert not ok and "nonce" in msg
    assert not task.done()  # the turn is still paused
    assert approvals.pending() == [pending]
    task.cancel()


async def test_nonce_is_single_use() -> None:
    cm = _cm()
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    task = await _start(approvals, _call())
    (pending,) = approvals.pending()
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    assert approvals.resolve(pending.decision_id, nonce, "approve")[0]
    await task
    # a replay of the consumed nonce cannot resolve anything
    ok, _ = approvals.resolve(pending.decision_id, nonce, "approve")
    assert not ok


async def test_fresh_nonce_retires_prior_nonce_on_same_connection() -> None:
    cm = _cm()
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    task = await _start(approvals, _call())
    (pending,) = approvals.pending()
    first = await approvals.mint_nonce(pending.decision_id, conn)
    second = await approvals.mint_nonce(pending.decision_id, conn)

    assert first is not None and second is not None and first != second
    assert not approvals.resolve(pending.decision_id, first, "approve")[0]
    assert approvals.resolve(pending.decision_id, second, "approve")[0]
    assert await task is Permission.ALLOW


async def test_nonce_refresh_preserves_other_live_connection() -> None:
    cm = _cm()
    first_conn = _conn(cm)
    second_conn = _conn(cm)
    approvals = ApprovalManager(cm)
    task = await _start(approvals, _call())
    (pending,) = approvals.pending()
    first = await approvals.mint_nonce(pending.decision_id, first_conn)
    second = await approvals.mint_nonce(pending.decision_id, second_conn)

    assert first is not None and second is not None
    assert approvals.resolve(pending.decision_id, first, "approve")[0]
    assert await task is Permission.ALLOW


async def test_nonce_from_dead_connection_refused() -> None:
    now = [0.0]
    cm = ConnectionManager(heartbeat_seconds=10.0, clock=lambda: now[0])
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    task = await _start(approvals, _call())
    (pending,) = approvals.pending()
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    now[0] = 30.0  # the minting client's heartbeat goes stale before the click
    ok, msg = approvals.resolve(pending.decision_id, nonce, "approve")
    assert not ok and "live" in msg
    assert not task.done()
    task.cancel()


async def test_invalidate_connection_drops_its_nonces() -> None:
    cm = _cm()
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    task = await _start(approvals, _call())
    (pending,) = approvals.pending()
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    approvals.invalidate_connection(conn)  # e.g. the socket dropped/reconnected
    ok, _ = approvals.resolve(pending.decision_id, nonce, "approve")
    assert not ok
    task.cancel()


async def test_mint_nonce_requires_pending_and_live() -> None:
    now = [0.0]
    cm = ConnectionManager(heartbeat_seconds=10.0, clock=lambda: now[0])
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    assert await approvals.mint_nonce("no-such-decision", conn) is None  # nothing pending
    task = await _start(approvals, _call())
    (pending,) = approvals.pending()
    now[0] = 30.0  # client no longer live
    assert await approvals.mint_nonce(pending.decision_id, conn) is None
    task.cancel()


async def test_fail_closed_denies_without_a_click() -> None:
    cm = _cm()
    _conn(cm)
    approvals = ApprovalManager(cm)
    task = await _start(approvals, _call())
    (pending,) = approvals.pending()
    approvals.fail(pending.decision_id)  # the voice-screen fail-closed path (Task 6)
    assert await task is Permission.DENY


# --- UIApprover parity: "always" persists exactly like the REPL ------------


async def test_ui_approver_always_persists_shell_prefix(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    gate = PermissionGate(load_policy(tmp_path / "config" / "permissions.yaml"), config.root)
    cm = _cm()
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    approver = UIApprover(approvals, gate, config)
    task = _task(approver(_call("run_shell", command="git status"), ASK))
    await asyncio.sleep(0)
    (pending,) = approvals.pending()
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    approvals.resolve(pending.decision_id, nonce, "always")
    assert await task is Permission.ALLOW
    # identical to the REPL's persist: a shell prefix rule now exists for "git status".
    assert any(r.prefix == "git status" for r in gate.policy.shell.rules)


async def test_ui_subagent_approver_labels_and_grants(tmp_path: Path) -> None:
    # A sub-agent ASK is surfaced with its title; "always" records a run-scoped pattern
    # grant on the child's gate (not persist_always). Here we just assert the labeling +
    # that resolve drives the future (grant path exercised in Phase 6 gate tests).
    from kira.permissions import SubAgentGate

    config = load_config(root=tmp_path, env_file=None)
    parent = PermissionGate(load_policy(tmp_path / "config" / "permissions.yaml"), config.root)
    sub_gate = SubAgentGate(parent, scope=frozenset({"web_fetch"}), project_root=config.root)
    cm = _cm()
    conn = _conn(cm)
    approvals = ApprovalManager(cm)
    approver = make_ui_subagent_approver(approvals, sub_gate, "a1", "researcher")
    task = _task(approver(_call("web_fetch", url="https://x.test"), ASK))
    await asyncio.sleep(0)
    (pending,) = approvals.pending()
    assert pending.kind == "subagent" and pending.title == "researcher"
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    approvals.resolve(pending.decision_id, nonce, "approve")
    assert await task is Permission.ALLOW


# --- Gate routes: auth + wiring --------------------------------------------


def _client(tmp_path: Path, *, base_url: str = "http://127.0.0.1"):
    config = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(config, auth=auth)
    return TestClient(app, base_url=base_url), app, auth


def _auth(auth: AuthManager) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}


def test_list_approvals_requires_session(tmp_path: Path) -> None:
    client, _app_, _auth_ = _client(tmp_path)
    assert client.get("/api/approvals").status_code == 401


def test_list_approvals_without_workspace_hides_pending_payloads(tmp_path: Path) -> None:
    client, app, auth = _client(tmp_path)
    # seed a pending approval directly (to_public doesn't touch the future)
    fut: asyncio.Future = asyncio.get_event_loop_policy().new_event_loop().create_future()
    from kira.ui.approver import PendingApproval

    app.state.approvals._pending["D1"] = PendingApproval(
        "D1", _call("write_file", path="a.txt"), ASK, "turn", None, fut, lambda: None, _CONTEXT
    )
    r = client.get("/api/approvals", headers=_auth(auth))
    assert r.status_code == 200
    assert r.json()["pending"] == []  # no server-bound workspace => no Gate payload disclosure


def test_resolve_requires_session_and_origin(tmp_path: Path) -> None:
    client, _app_, auth = _client(tmp_path)
    # no session ⇒ 401
    r = client.post(
        "/api/approvals/D1/resolve",
        json={"nonce": "n", "action": "approve"},
        headers={"origin": "http://127.0.0.1"},
    )
    assert r.status_code == 401
    # foreign origin ⇒ 403 (CSRF wall), even with a session
    r = client.post(
        "/api/approvals/D1/resolve",
        json={"nonce": "n", "action": "approve"},
        headers={**_auth(auth), "origin": "http://evil.com"},
    )
    assert r.status_code == 403
    # Another loopback port is also foreign to this exact UI origin. SameSite cookies are not
    # port-scoped, so this must stop before the Gate's resolve route sees the request.
    r = client.post(
        "/api/approvals/D1/resolve",
        json={"nonce": "n", "action": "approve"},
        headers={**_auth(auth), "origin": "http://127.0.0.1:3000"},
    )
    assert r.status_code == 403


@pytest.mark.parametrize("action", ["approve", "always", "deny"])
def test_resolve_bad_nonce_returns_409(tmp_path: Path, action: str) -> None:
    client, _app_, auth = _client(tmp_path)
    r = client.post(
        "/api/approvals/nope/resolve",
        json={"nonce": "bogus", "action": action},
        headers={**_auth(auth), "origin": "http://127.0.0.1"},
    )
    assert r.status_code == 409 and r.json()["ok"] is False


def test_gate_resolve_exact_loopback_origin_with_port_reaches_handler(tmp_path: Path) -> None:
    client, _app_, auth = _client(tmp_path, base_url="http://127.0.0.1:8787")
    r = client.post(
        "/api/approvals/nope/resolve",
        json={"nonce": "bogus", "action": "approve"},
        headers={**_auth(auth), "origin": "http://127.0.0.1:8787"},
    )
    assert r.status_code == 409 and r.json()["ok"] is False


async def test_resolve_route_happy_path(tmp_path: Path) -> None:
    # End-to-end over ASGI in one event loop: a live WS connection, a pending approval,
    # a minted nonce, then the real POST route resolves it.
    config = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(config, auth=auth)
    approvals, cm = app.state.approvals, app.state.connections
    conn = _conn(cm)
    task = _task(approvals.request(_call(), ASK, kind="turn", title=None, on_always=lambda: None))
    await asyncio.sleep(0)
    (pending,) = approvals.pending()
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    sid = auth.mint_session()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        r = await ac.post(
            f"/api/approvals/{pending.decision_id}/resolve",
            json={"nonce": nonce, "action": "approve"},
            headers={"cookie": f"{SESSION_COOKIE}={sid}", "origin": "http://127.0.0.1"},
        )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert await task is Permission.ALLOW


def test_gate_policy_and_audit_routes(tmp_path: Path) -> None:
    client, _app_, auth = _client(tmp_path)
    pol = client.get("/api/gate/policy", headers=_auth(auth))
    assert pol.status_code == 200 and "policy" in pol.json()
    aud = client.get("/api/audit/today", headers=_auth(auth))
    assert aud.status_code == 200 and "events" in aud.json()
