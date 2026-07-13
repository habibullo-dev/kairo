"""capability_truth: the ONE availability read model (Phase 15.5).

Daily, Hub, Settings, and the conversation header all render this single function, so they can
never disagree about what's connected or usable. The valuable extra is honesty: a CONNECTED thing
that isn't wired into the chat says so (a notifier is not a chat tool; an external provider would
receive private context; a connected connector whose tool didn't register). Presence/state/reason
only — never a key value. Keyless (pure function over config + a connectors dict)."""

from __future__ import annotations

from pathlib import Path

from jarvis.config import load_config
from jarvis.ui.readmodels import capability_truth


def _cfg(tmp_path: Path):
    return load_config(root=tmp_path, env_file=None)


def _row(rows: list[dict], name: str) -> dict:
    return next(r for r in rows if r["name"] == name)


def test_no_connectors_reads_as_not_configured(tmp_path: Path) -> None:
    cap = capability_truth(_cfg(tmp_path), connectors=None, voice={"enabled": False})
    for label in ("Gmail", "Google Drive", "Google Calendar"):
        r = _row(cap["connectors"], label)
        assert r["state"] == "not_configured" and r["exposed_to_chat"] is False and r["reason"]
    # anthropic is the chat; external providers are visible but not exposed (private context)
    assert _row(cap["providers"], "Anthropic")["exposed_to_chat"] is True
    assert all(
        p["exposed_to_chat"] is False for p in cap["providers"] if p["name"] != "Anthropic"
    )
    assert cap["mcp"]["exposed_to_chat"] is False  # no MCP client yet
    assert cap["voice"]["state"] == "off" and cap["voice"]["reason"]
    telegram = _row(cap["connectors"], "Telegram")
    assert telegram["reason"] == (
        "Not configured. Approved sends and digest delivery are separately configured."
    )


def test_connected_google_is_exposed_when_tools_registered(tmp_path: Path) -> None:
    connectors = {"demo": False, "google": {"connected": True, "needs_reconnect": False},
                  "notifiers": {"telegram": {"configured": True}}}
    registered = {"gmail_search", "drive_search", "calendar_list_events", "read_file"}
    cap = capability_truth(_cfg(tmp_path), connectors=connectors, voice={"enabled": False},
                           registered_tools=registered)
    for label in ("Gmail", "Google Drive", "Google Calendar"):
        r = _row(cap["connectors"], label)
        assert r["state"] == "connected" and r["exposed_to_chat"] is True and r["reason"] == ""
    # a notifier is CONNECTED but is not a chat tool — the honest "connected, not exposed" case
    tg = _row(cap["connectors"], "Telegram")
    assert tg["state"] == "connected" and tg["exposed_to_chat"] is False and "notification" in \
        tg["reason"].lower()


def test_connected_but_tool_not_registered_explains_why(tmp_path: Path) -> None:
    # Google is connected, but the gmail tool didn't register (e.g. a missing scope / build gap):
    # the row must say connected-but-not-exposed rather than a false green.
    connectors = {"demo": False, "google": {"connected": True, "needs_reconnect": False},
                  "notifiers": {}}
    cap = capability_truth(_cfg(tmp_path), connectors=connectors, voice={"enabled": False},
                           registered_tools={"drive_search"})  # gmail/calendar absent
    gmail = _row(cap["connectors"], "Gmail")
    assert gmail["state"] == "connected" and gmail["exposed_to_chat"] is False and gmail["reason"]
    assert _row(cap["connectors"], "Google Drive")["exposed_to_chat"] is True


def test_needs_reconnect_surfaces(tmp_path: Path) -> None:
    connectors = {"demo": False, "google": {"connected": True, "needs_reconnect": True},
                  "notifiers": {}}
    cap = capability_truth(_cfg(tmp_path), connectors=connectors, voice={"enabled": True})
    gmail = _row(cap["connectors"], "Gmail")
    assert gmail["state"] == "needs_reconnect" and "reconnect" in gmail["reason"].lower()
    assert cap["voice"]["state"] == "on" and cap["voice"]["exposed_to_chat"] is True


def test_summary_is_a_short_plain_string(tmp_path: Path) -> None:
    cap = capability_truth(_cfg(tmp_path), connectors=None, voice={"enabled": False})
    assert isinstance(cap["summary"], str) and "voice off" in cap["summary"]


def test_resilient_to_provider_registry_failure(tmp_path: Path, monkeypatch) -> None:
    # A pricing/provider hiccup must NEVER blank the grid or 500 the surface (the Checkpoint-J2
    # root cause: a throwing read model → empty Hub / empty selector). The connector rows + voice +
    # MCP always render; providers degrade to just anthropic.
    import jarvis.models.providers as prov

    def boom(*_a, **_k):
        raise RuntimeError("pricing table exploded")

    monkeypatch.setattr(prov.ProviderRegistry, "from_config", classmethod(boom))
    monkeypatch.setattr("jarvis.ui.readmodels.services_status", boom)
    cap = capability_truth(_cfg(tmp_path), connectors=None, voice={"enabled": False})
    assert len(cap["connectors"]) == 5  # ALWAYS the five connector rows
    assert cap["providers"][0]["name"] == "Anthropic"  # anthropic still shown (the main chat)
    assert cap["services"] == [] and "summary" in cap  # services degrade; no crash
