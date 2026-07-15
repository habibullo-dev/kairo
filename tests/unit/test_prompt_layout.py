"""Stable-first prompt layout + prefix hashes (S7.2). Pure, deterministic, keyless.

Pins: stable-before-volatile ordering (canonical), hash determinism, that changing one stable
section busts only that component hash + the composite (never a sibling), and the sensitivity flag.
"""

from __future__ import annotations

from kira.models.prompt_layout import (
    STABLE_ORDER,
    VOLATILE_ORDER,
    AssembledPrompt,
    PromptSection,
    SectionKind,
    assemble,
)


def _sec(kind: SectionKind, text: str, sensitive: bool = False) -> PromptSection:
    return PromptSection(kind=kind, text=text, sensitive=sensitive)


def _base() -> list[PromptSection]:
    # Deliberately shuffled — volatile before stable, stable out of canonical order.
    return [
        _sec(SectionKind.MEMORY_RECALL, "recall: last week you..."),
        _sec(SectionKind.TOOL_SCHEMAS, "tools: read_file, calendar_create_event"),
        _sec(SectionKind.SYSTEM_CONTRACT, "You are Kira. Safety contract..."),
        _sec(SectionKind.USER_TURN, "what's on my calendar?"),
        _sec(SectionKind.PROJECT_POLICY, "project Kira: local-first"),
        _sec(SectionKind.SERVICE_CATALOG, "services: semgrep, gitleaks"),
        _sec(SectionKind.TEAM_PROFILES, "teams: research, frontend"),
        _sec(SectionKind.CURRENT_TIME, "2026-07-08T22:00"),
    ]


def test_orders_stable_first_then_volatile_canonically() -> None:
    out = assemble(_base())
    kinds = [s.kind for s in out.sections]
    stable_seen = [k for k in kinds if k in STABLE_ORDER]
    volatile_seen = [k for k in kinds if k in VOLATILE_ORDER]
    # all stable come before any volatile
    assert kinds[: len(stable_seen)] == stable_seen
    # each half is in canonical order
    assert stable_seen == [k for k in STABLE_ORDER if k in stable_seen]
    assert volatile_seen == [k for k in VOLATILE_ORDER if k in volatile_seen]
    # the stable text leads; the user turn is NOT in the stable prefix
    assert out.stable_text.startswith("You are Kira")
    assert "what's on my calendar" not in out.stable_text
    assert "what's on my calendar" in out.volatile_text


def test_all_five_named_hashes_plus_composite() -> None:
    out = assemble(_base())
    assert set(out.hashes) == {
        "system_contract_hash", "tool_schema_hash", "team_profile_hash",
        "service_catalog_hash", "project_policy_hash", "stable_prefix_hash",
    }
    assert out.stable_prefix_hash == out.hashes["stable_prefix_hash"]


def test_hashing_is_deterministic() -> None:
    assert assemble(_base()).hashes == assemble(_base()).hashes  # same input ⇒ same hashes


def test_changing_one_stable_section_busts_only_it_and_the_composite() -> None:
    a = assemble(_base())
    changed = _base()
    changed = [
        _sec(s.kind, "tools: read_file, calendar_create_event, drive_create_doc")
        if s.kind is SectionKind.TOOL_SCHEMAS else s
        for s in changed
    ]
    b = assemble(changed)
    assert b.hashes["tool_schema_hash"] != a.hashes["tool_schema_hash"]  # busted
    assert b.hashes["stable_prefix_hash"] != a.hashes["stable_prefix_hash"]  # composite busted
    # siblings unchanged
    assert b.hashes["system_contract_hash"] == a.hashes["system_contract_hash"]
    assert b.hashes["team_profile_hash"] == a.hashes["team_profile_hash"]


def test_changing_only_volatile_does_not_bust_the_prefix() -> None:
    a = assemble(_base())
    changed = [
        _sec(s.kind, "totally different question") if s.kind is SectionKind.USER_TURN else s
        for s in _base()
    ]
    b = assemble(changed)
    assert b.stable_prefix_hash == a.stable_prefix_hash  # volatile change ⇒ prefix still reusable


def test_sensitivity_flag_marks_the_prefix() -> None:
    clean = assemble(_base())
    assert clean.stable_is_sensitive is False
    tainted = assemble(
        [_sec(SectionKind.PROJECT_POLICY, "SECRET project detail", sensitive=True), *_base()]
    )
    assert tainted.stable_is_sensitive is True  # a sensitive stable section flags the prefix


def test_returns_assembled_prompt_type() -> None:
    assert isinstance(assemble([]), AssembledPrompt)
    empty = assemble([])
    assert empty.stable_text == "" and empty.volatile_text == ""
    assert len(empty.hashes) == 6  # five named + composite, even when empty
