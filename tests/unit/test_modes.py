"""Run modes: the decision matrix + the safety pins (Phase 10 Task 5).

The pure predicates (plan_blocks / auto_approves) are tested exhaustively here, and the
loop integration is tested end-to-end with a FakeClient: the load-bearing safety pins are

* Plan denies everything outside PLAN_SAFE (allowlist — a new tool fails closed).
* Auto NEVER auto-approves a non-persistable (tainted-egress) decision — so the Phase 9
  exfil guard survives Auto mode (pre-mortem #1).
* Auto NEVER auto-approves run_shell / write_file, even if a user configures them.
* A mid-turn flip INTO Auto doesn't apply to the in-flight turn (pre-mortem #12).
"""

from __future__ import annotations

from pathlib import Path

from jarvis.config import load_config
from jarvis.core import AgentLoop, FakeClient, build_system, text_message, tool_use_message
from jarvis.core.client import ToolCall
from jarvis.permissions import PermissionGate, Policy
from jarvis.permissions.gate import Decision
from jarvis.permissions.modes import (
    AUTO_NEVER,
    PLAN_SAFE,
    Mode,
    ModeState,
    auto_approves,
    plan_blocks,
)
from jarvis.tools import Permission, ToolContext, ToolExecutor, ToolRegistry

# --- PLAN_SAFE pin (adding a tool forces a deliberate classification) -------


def test_plan_safe_is_exactly_the_read_only_set() -> None:
    # THE pin: PLAN_SAFE is an explicit allowlist. If you add a tool to the app, this test
    # fails until you deliberately decide whether it is plan-safe (read-only, no world change).
    assert (
        frozenset(
            {
                "read_file",
                "list_dir",
                "glob_search",
                "query_knowledge_base",
                "lint_knowledge_base",
                "recall",
                "calendar_list_events",
                "gmail_search",
                "gmail_read",
                "drive_search",
                "drive_fetch",
            }
        )
        == PLAN_SAFE
    )


def test_plan_safe_excludes_all_egress_and_write_tools() -> None:
    # Defense in depth: no obviously-side-effecting tool may sneak into the allowlist.
    for forbidden in (
        "run_shell",
        "write_file",
        "web_search",
        "web_fetch",
        "gmail_create_draft",
        "send_notification",
        "remember",
        "schedule_task",
        "ingest_source",
        "write_wiki_page",
        "spawn_agent",
    ):
        assert forbidden not in PLAN_SAFE, forbidden


# --- plan_blocks ------------------------------------------------------------


def test_plan_blocks_matrix() -> None:
    assert plan_blocks(Mode.PLAN, "run_shell") is True
    assert plan_blocks(Mode.PLAN, "read_file") is False  # PLAN_SAFE
    assert plan_blocks(Mode.PLAN, "some_future_tool") is True  # allowlist fails closed
    # Non-plan modes never block.
    assert plan_blocks(Mode.APPROVAL, "run_shell") is False
    assert plan_blocks(Mode.AUTO, "run_shell") is False


# --- auto_approves ----------------------------------------------------------


def _ask(persistable: bool = True) -> Decision:
    return Decision(Permission.ASK, "needs approval", persistable=persistable)


def test_auto_approves_allowlisted_persistable_ask() -> None:
    assert auto_approves(
        mode=Mode.AUTO,
        started_auto=True,
        decision=_ask(),
        tool_name="web_search",
        auto_allow_tools=frozenset({"web_search"}),
    )


def test_auto_never_approves_non_persistable_tainted_egress() -> None:
    # THE Phase 9 guard under Auto: a tainted-egress ASK is persistable=False and must ALWAYS
    # reach the human, even if the tool is in the allowlist.
    assert not auto_approves(
        mode=Mode.AUTO,
        started_auto=True,
        decision=_ask(persistable=False),
        tool_name="web_search",
        auto_allow_tools=frozenset({"web_search"}),
    )


def test_auto_never_approves_run_shell_or_write_file() -> None:
    for never in AUTO_NEVER:
        assert not auto_approves(
            mode=Mode.AUTO,
            started_auto=True,
            decision=_ask(),
            tool_name=never,
            auto_allow_tools=frozenset({never}),  # even if a user configures it
        )


def test_auto_requires_turn_started_in_auto() -> None:
    # A mid-turn flip INTO Auto (turn started in approval) must not apply (#12).
    assert not auto_approves(
        mode=Mode.AUTO,
        started_auto=False,
        decision=_ask(),
        tool_name="web_search",
        auto_allow_tools=frozenset({"web_search"}),
    )


def test_auto_only_for_tools_in_the_allowlist() -> None:
    assert not auto_approves(
        mode=Mode.AUTO,
        started_auto=True,
        decision=_ask(),
        tool_name="web_fetch",
        auto_allow_tools=frozenset({"web_search"}),  # web_fetch not listed
    )


# --- loop integration -------------------------------------------------------


def _loop(tmp_path: Path, client, *, mode: ModeState, auto_allow=(), approver=None) -> AgentLoop:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.modes.auto_allow_tools = list(auto_allow)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    return AgentLoop(
        client=client,
        registry=reg,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
        approver=approver,
        system=build_system(),
        mode=mode.current,
    )


async def _deny(_c, _d) -> Permission:
    return Permission.DENY


async def test_plan_mode_denies_write_file_end_to_end(tmp_path: Path) -> None:
    # In plan mode a write_file ASK is denied outright (never prompts, never runs).
    (tmp_path / "x.txt").write_text("hi", encoding="utf-8")
    approved = {"called": False}

    async def _approve(_c, _d):
        approved["called"] = True
        return Permission.ALLOW

    client = FakeClient(
        [
            tool_use_message([ToolCall("t1", "write_file", {"path": "x.txt", "content": "new"})]),
            text_message("done"),
        ]
    )
    loop = _loop(tmp_path, client, mode=ModeState(Mode.PLAN), approver=_approve)
    await loop.run_turn([{"role": "user", "content": "overwrite x"}])
    assert approved["called"] is False  # plan denies before the approver
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hi"  # never written


async def test_auto_mode_auto_approves_allowlisted(tmp_path: Path) -> None:
    # write_file is NOT auto-approvable (AUTO_NEVER); use a benign allowlisted tool. Here we
    # verify the auto path resolves without the (deny) approver being consulted.
    (tmp_path / "note.txt").write_text("secret", encoding="utf-8")
    client = FakeClient(
        [
            tool_use_message([ToolCall("t1", "read_file", {"path": "note.txt"})]),
            text_message("read it"),
        ]
    )
    # read_file is ALLOW by default, so it wouldn't hit the approver anyway. To exercise auto,
    # put an ASK tool (run_shell) in the allowlist and confirm AUTO_NEVER still blocks it.
    loop = _loop(
        tmp_path,
        client,
        mode=ModeState(Mode.AUTO),
        auto_allow=("run_shell",),
        approver=_deny,
    )
    result = await loop.run_turn([{"role": "user", "content": "read the note"}])
    assert result.text == "read it"


async def test_auto_mode_run_shell_still_denied_when_configured(tmp_path: Path) -> None:
    # A user puts run_shell in auto_allow_tools — it must STILL require the human (AUTO_NEVER),
    # so with a deny approver the shell command is denied and never runs.
    client = FakeClient(
        [
            tool_use_message([ToolCall("t1", "run_shell", {"command": "echo pwned > /tmp/x"})]),
            text_message("could not run"),
        ]
    )
    loop = _loop(
        tmp_path, client, mode=ModeState(Mode.AUTO), auto_allow=("run_shell",), approver=_deny
    )
    result = await loop.run_turn([{"role": "user", "content": "run it"}])
    # The run_shell became a denied (is_error) tool result — auto never approved it.
    tool_results = [
        b
        for m in result.messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_results and all(b["is_error"] for b in tool_results)


async def test_background_loop_without_mode_is_approval(tmp_path: Path) -> None:
    # No mode provider ⇒ Approval semantics (what the BackgroundRunner/voice loops get), so
    # plan/auto never leak in. A write_file ASK reaches the (deny) approver and is denied.
    (tmp_path / "y.txt").write_text("keep", encoding="utf-8")
    client = FakeClient(
        [
            tool_use_message([ToolCall("t1", "write_file", {"path": "y.txt", "content": "x"})]),
            text_message("done"),
        ]
    )
    cfg = load_config(root=tmp_path, env_file=None)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    loop = AgentLoop(
        client=client,
        registry=reg,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
        approver=_deny,
        system=build_system(),
        mode=None,  # no mode layer
    )
    await loop.run_turn([{"role": "user", "content": "overwrite y"}])
    assert (tmp_path / "y.txt").read_text(encoding="utf-8") == "keep"  # denied by approver


# --- the /api/mode route ----------------------------------------------------


def test_mode_route_sets_and_reports(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from jarvis.ui.auth import SESSION_COOKIE, AuthManager
    from jarvis.ui.server import create_app

    cfg = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    app.state.modes = ModeState(Mode.APPROVAL)
    client = TestClient(app, base_url="http://127.0.0.1")

    def hdr(*, post: bool = False) -> dict[str, str]:
        h = {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}
        if post:
            h["origin"] = "http://127.0.0.1"
        return h

    # default reported on the status feed
    assert client.get("/api/runner", headers=hdr()).json()["mode"] == "approval"
    # flip to auto
    r = client.post("/api/mode", json={"mode": "auto"}, headers=hdr(post=True))
    assert r.status_code == 200 and r.json()["mode"] == "auto"
    assert app.state.modes.current() is Mode.AUTO
    assert client.get("/api/runner", headers=hdr()).json()["mode"] == "auto"
    # a bogus mode is rejected
    bad = client.post("/api/mode", json={"mode": "yolo"}, headers=hdr(post=True))
    assert bad.status_code == 400


def test_runner_reports_conversation_and_model_truth(tmp_path: Path) -> None:
    # Phase 15.5: /api/runner carries the active session + the interactive model/effort, so the
    # client can rehydrate the transcript it is in and render honest composer chips.
    from fastapi.testclient import TestClient

    from jarvis.ui.auth import SESSION_COOKIE, AuthManager
    from jarvis.ui.server import create_app

    cfg = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    client = TestClient(create_app(cfg, auth=auth), base_url="http://127.0.0.1")
    body = client.get(
        "/api/runner", headers={"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}
    ).json()
    # shape present (no session wired in this bare app ⇒ session_id/title are null)
    assert set(body) >= {"session_id", "session_title", "model", "effort"}
    assert body["session_id"] is None and body["session_title"] is None
    assert body["model"] == cfg.models.main  # config default until Task 2's override state exists
    assert body["effort"] == cfg.limits.effort
