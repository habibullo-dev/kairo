"""Attention routing matrix + minimized pushes (Phase 16 Task 4).

The load-bearing pins: urgent pushes are COUNT-ONLY (no titles/bodies — an email subject can never
leak); egress is opt-in (empty urgent_channels ⇒ nothing sent); quiet hours + per-project mute only
SUPPRESS a push (fold to digest), never escalate. Keyless with a fake notifier."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from jarvis.attention.routing import (
    NotificationRouter,
    in_quiet_hours,
    minimized_push,
    notify_attention_counts,
    notify_open_attention_item,
    route_notification,
)
from jarvis.config import load_config


def _route(priority, **kw):
    base = dict(
        priority=priority, project_id=1, hour=12, urgent_channels=["telegram"],
        normal_channels=[], low_channels=[],
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


def test_each_priority_can_explicitly_push_to_telegram() -> None:
    normal = _route("normal", normal_channels=["telegram"])
    low = _route("low", low_channels=["telegram"])
    assert normal.channels == ("telegram",) and normal.to_digest is True
    assert low.channels == ("telegram",) and low.to_digest is False


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


@pytest.mark.parametrize(
    "counts",
    [
        {"task title / CANARY": 1},
        {"approval": -1},
        {"approval": True},
    ],
)
def test_minimized_push_rejects_any_untrusted_label_or_count(counts: dict[str, int]) -> None:
    # Labels are rendered into off-box text, so the count API has a closed, non-sensitive schema.
    with pytest.raises(ValueError):
        minimized_push(counts)


# --- router side effect (sends the minimized text via the notifier) --------
class _FakeNotifier:
    def __init__(self): self.sent: list[str] = []
    async def send(self, text: str) -> None: self.sent.append(text)


class _FakeConnectors:
    def __init__(self, notifier): self._n = notifier
    def notifier(self, _channel): return self._n


class _FakeItem:
    priority = "normal"
    project_id = 7
    state = "open"


class _FakeAttention:
    async def get(self, item_id):
        assert item_id == 11
        return _FakeItem()

    async def open_counts(self, *, project_id):
        assert project_id == 7
        return {"approval": 2}


async def test_router_sends_minimized_only_for_urgent_enabled(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.attention = cfg.attention.model_copy(update={"urgent_channels": ["telegram"]})
    n = _FakeNotifier()
    router = NotificationRouter(cfg, _FakeConnectors(n))
    d = await router.notify(priority="urgent", project_id=None,
                            open_counts={"approval": 2, "alert": 1}, hour=12)
    assert d.channels == ("telegram",)
    assert n.sent == ["Kairo · 3 need you: 1 alert, 2 approvals"]  # counts only (sorted), no bodies
    # A normal item keeps its legacy digest-only behavior until explicitly enabled.
    await router.notify(priority="normal", project_id=None, open_counts={"approval": 1}, hour=12)
    assert len(n.sent) == 1


async def test_router_deduplicates_bypassed_config_channel_entries(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    # model_copy deliberately bypasses Pydantic validation; routing remains defensive anyway.
    cfg.attention = cfg.attention.model_copy(update={"urgent_channels": ["telegram", "telegram"]})
    n = _FakeNotifier()
    await NotificationRouter(cfg, _FakeConnectors(n)).notify(
        priority="urgent", project_id=None, open_counts={"approval": 1}, hour=12
    )
    assert n.sent == ["Kairo · 1 needs you: 1 approval"]


async def test_default_config_never_pushes_fatigue_safe(tmp_path: Path) -> None:
    # Priority discipline / notification fatigue: with the DEFAULT config (urgent_channels empty),
    # NOTHING is pushed — even an urgent item stays center-only. Egress is a deliberate opt-in.
    cfg = load_config(root=tmp_path, env_file=None)
    assert cfg.attention.urgent_channels == []
    n = _FakeNotifier()
    router = NotificationRouter(cfg, _FakeConnectors(n))
    for priority in ("urgent", "normal", "low"):
        await router.notify(
            priority=priority, project_id=None, open_counts={"approval": 5}, hour=12
        )
    assert n.sent == []  # zero pushes by default — the human is never spammed out of the box


async def test_post_commit_helper_reads_counts_not_attention_content(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.attention = cfg.attention.model_copy(update={"normal_channels": ["telegram"]})
    n = _FakeNotifier()
    router = NotificationRouter(cfg, _FakeConnectors(n))
    await notify_open_attention_item(router, _FakeAttention(), 11)
    assert n.sent == ["Kairo · 2 need you: 2 approvals"]


async def test_count_helper_cannot_receive_a_title_or_payload(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.attention = cfg.attention.model_copy(update={"urgent_channels": ["telegram"]})
    n = _FakeNotifier()
    router = NotificationRouter(cfg, _FakeConnectors(n))
    await notify_attention_counts(
        router,
        priority="urgent",
        project_id=7,
        counts={"approval": 1},
        now=dt.datetime(2026, 7, 13, 12),
    )
    assert n.sent == ["Kairo · 1 needs you: 1 approval"]


class _BrokenNotifier:
    async def send(self, _text: str) -> None:
        raise RuntimeError("provider bug")


async def test_failed_best_effort_push_cannot_break_the_durable_flow(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.attention = cfg.attention.model_copy(update={"urgent_channels": ["telegram"]})
    router = NotificationRouter(cfg, _FakeConnectors(_BrokenNotifier()))
    decision = await notify_attention_counts(
        router, priority="urgent", project_id=None, counts={"approval": 1}
    )
    assert decision is not None and decision.channels == ("telegram",)


class _FakeNotices:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, int | None]] = []

    def post(self, text: str, *, kind: str, project_id: int | None) -> None:
        self.items.append((text, kind, project_id))


async def test_failed_push_posts_a_local_warning_after_host_wiring(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.attention = cfg.attention.model_copy(update={"urgent_channels": ["telegram"]})
    notices = _FakeNotices()
    router = NotificationRouter(cfg, _FakeConnectors(_BrokenNotifier()))
    router.set_notices(notices)
    await router.notify(priority="urgent", project_id=4, open_counts={"approval": 1}, hour=12)
    assert notices.items == [("Attention push to telegram failed.", "warn", 4)]


def test_config_rejects_unknown_urgent_channel() -> None:
    # Construct directly so the field_validator runs (model_copy skips validation). An unknown
    # channel is refused ⇒ no accidental egress to an unsupported sink.
    from pydantic import ValidationError

    from jarvis.config import AttentionConfig

    AttentionConfig(normal_channels=["telegram", "kakao"])  # the allowed set is fine
    with pytest.raises(ValidationError):
        AttentionConfig(low_channels=["email"])
    with pytest.raises(ValidationError):
        AttentionConfig(urgent_channels=["telegram", "telegram"])
