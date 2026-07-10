"""Attention routing matrix + minimized pushes (Phase 16 Task 4).

The load-bearing pins: urgent pushes are COUNT-ONLY (no titles/bodies — an email subject can never
leak); egress is opt-in (empty urgent_channels ⇒ nothing sent); quiet hours + per-project mute only
SUPPRESS a push (fold to digest), never escalate. Keyless with a fake notifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.attention.routing import (
    NotificationRouter,
    in_quiet_hours,
    minimized_push,
    route_notification,
)
from jarvis.config import load_config


def _route(priority, **kw):
    base = dict(
        priority=priority, project_id=1, hour=12, urgent_channels=["telegram"],
        quiet_start=None, quiet_end=None, muted_projects=[],
    )
    base.update(kw)
    return route_notification(**base)


# --- the matrix ------------------------------------------------------------
def test_urgent_pushes_when_enabled_and_awake_and_unmuted() -> None:
    d = _route("urgent")
    assert d.channels == ("telegram",) and d.to_digest is False


def test_urgent_folds_to_digest_when_muted() -> None:
    d = _route("urgent", muted_projects=[1])
    assert d.channels == () and d.to_digest is True


def test_urgent_folds_to_digest_in_quiet_hours() -> None:
    d = _route("urgent", hour=23, quiet_start=22, quiet_end=7)
    assert d.channels == () and d.to_digest is True


def test_urgent_with_no_channels_configured_pushes_nothing() -> None:
    # Opt-in egress: empty urgent_channels ⇒ no push (channels empty), and NOT folded to digest
    # either — it stays center-only. Nothing crosses the wire until a channel is enabled.
    d = _route("urgent", urgent_channels=[])
    assert d.channels == () and d.to_digest is False


def test_normal_goes_to_digest_and_low_is_center_only() -> None:
    assert _route("normal", project_id=None).to_digest is True
    low = _route("low", project_id=None)
    assert low.channels == () and low.to_digest is False


# --- quiet hours (incl. midnight wrap) -------------------------------------
def test_quiet_hours_wrap_midnight() -> None:
    assert in_quiet_hours(23, 22, 7) is True
    assert in_quiet_hours(3, 22, 7) is True
    assert in_quiet_hours(12, 22, 7) is False
    assert in_quiet_hours(12, None, None) is False  # no window ⇒ never quiet


# --- minimized push: counts only, never a body -----------------------------
def test_minimized_push_is_counts_only() -> None:
    text = minimized_push({"approval": 2, "proposal": 1})
    assert text == "Kairo · 3 need you: 2 approvals, 1 proposal"


def test_minimized_push_never_contains_item_content() -> None:
    # Even if a caller somehow had sensitive titles, the push is derived ONLY from counts — the
    # function takes counts, not items, so a subject/body cannot appear by construction.
    text = minimized_push({"alert": 1})
    assert text == "Kairo · 1 needs you: 1 alert"
    assert "@" not in text and "http" not in text  # no address/URL could ever be here


# --- router side effect (sends the minimized text via the notifier) --------
class _FakeNotifier:
    def __init__(self): self.sent: list[str] = []
    async def send(self, text: str) -> None: self.sent.append(text)


class _FakeConnectors:
    def __init__(self, notifier): self._n = notifier
    def notifier(self, _channel): return self._n


async def test_router_sends_minimized_only_for_urgent_enabled(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.attention = cfg.attention.model_copy(update={"urgent_channels": ["telegram"]})
    n = _FakeNotifier()
    router = NotificationRouter(cfg, _FakeConnectors(n))
    d = await router.notify(priority="urgent", project_id=None,
                            open_counts={"approval": 2, "alert": 1}, hour=12)
    assert d.channels == ("telegram",)
    assert n.sent == ["Kairo · 3 need you: 1 alert, 2 approvals"]  # counts only (sorted), no bodies
    # a normal item never pushes
    await router.notify(priority="normal", project_id=None, open_counts={"approval": 1}, hour=12)
    assert len(n.sent) == 1


def test_config_rejects_unknown_urgent_channel() -> None:
    # Construct directly so the field_validator runs (model_copy skips validation). An unknown
    # channel is refused ⇒ no accidental egress to an unsupported sink.
    from pydantic import ValidationError

    from jarvis.config import AttentionConfig

    AttentionConfig(urgent_channels=["telegram", "kakao"])  # the allowed set is fine
    with pytest.raises(ValidationError):
        AttentionConfig(urgent_channels=["email"])
