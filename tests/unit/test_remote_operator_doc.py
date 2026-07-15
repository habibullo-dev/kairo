"""Contract pins for the current Kira Remote Operator documentation."""

import re
from pathlib import Path

from jarvis.config import TelegramRemoteControlConfig, TelegramRemoteOperatorConfig

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "REMOTE-OPERATOR.md").read_text(encoding="utf-8")
NORMALIZED = " ".join(DOC.split())


def test_remote_operator_doc_uses_current_kira_identity_and_commands() -> None:
    assert "# Kira Telegram Remote Operator" in DOC
    assert "uv run kira --ui" in DOC
    assert "uv run kira connect status" in DOC
    assert "uv run kira connect telegram" in DOC
    assert re.search(r"\b(?:cairo|kairo|jarvis)\b", DOC, flags=re.IGNORECASE) is None

    for command in (
        "/status",
        "/tasks",
        "/inbox [filter]",
        "/calendar",
        "/briefing",
        "/clear",
        "/projects",
        "/jobs",
        "/approvals",
        "/approve CODE",
        "/deny CODE",
        "/cancel ID",
        "/news-pdf [public topic]",
    ):
        assert command in DOC


def test_remote_operator_doc_tracks_runtime_defaults() -> None:
    remote = TelegramRemoteControlConfig()
    operator = TelegramRemoteOperatorConfig()

    for claim in (
        f"at most {remote.conversation_context_turns} delivered turns",
        f"{remote.conversation_context_max_chars:,} combined characters",
        f"{remote.reference_context_ttl_minutes}-minute reference TTL",
        f"{remote.max_model_messages_per_hour} model messages per hour",
        f"{remote.max_read_requests_per_hour} requests per hour",
        f"after {operator.proposal_ttl_minutes} minutes",
        f"after {operator.approval_ttl_minutes} minutes",
        f"defaults to {operator.default_status_interval_minutes} minutes",
        f"more than {operator.max_active_jobs} approved Remote Operator jobs",
        f"{operator.live_web_search_max_results} or fewer",
    ):
        assert claim in NORMALIZED

    for tool in operator.allowed_tools:
        assert f"`{tool}`" in DOC


def test_remote_operator_doc_pins_authority_and_recovery_boundaries() -> None:
    for claim in (
        "Remote chat and Remote Operator are separate opt-ins",
        "conversation text is not written to SQLite",
        "A project is not a filesystem sandbox",
        "each search is ALLOW egress with no per-query approval or semantic data-loss-prevention",
        "Attachment turns remain non-egressing even when text-chat live search is enabled",
        "atomically consumes the code once and transitions the exact proposal",
        "then attempts to schedule and durably bind the task",
        "before any tool in that assistant batch executes",
        "Telegram approval previews are intentionally minimized, not full diffs",
        "it is not a full content-review surface",
        "where `ID` is the proposal/job number shown by `/jobs`, not the scheduler task id",
        "Standing allows for side-effecting tools are demoted to exact-call asks",
        "At every controller start, Kira discards the entire Telegram backlog",
        "resend the request or use `/approvals`",
        "marks approved-but-unbound proposals failed",
        "never replays a Telegram message, proposal, or unapproved tool call",
    ):
        assert claim in NORMALIZED
