"""Shared system-prompt behavior for primary and delegated assistants."""

from __future__ import annotations

from jarvis.core.prompts import (
    COLLABORATION_GUIDANCE,
    DEFAULT_IDENTITY,
    SUBAGENT_GUIDANCE,
    build_system,
)


def test_shared_collaboration_guidance_is_always_present() -> None:
    primary = build_system()
    delegated = build_system(subagent=True)

    assert COLLABORATION_GUIDANCE in primary
    assert COLLABORATION_GUIDANCE in delegated
    assert primary.count(COLLABORATION_GUIDANCE) == 1
    assert delegated.count(COLLABORATION_GUIDANCE) == 1


def test_collaboration_guidance_precedes_role_and_dynamic_context() -> None:
    prompt = build_system(
        subagent=True,
        skills="REVIEWED-ROLE-GUIDANCE",
        extra="VOLATILE-CONTEXT",
    )

    assert prompt.index(DEFAULT_IDENTITY) < prompt.index(COLLABORATION_GUIDANCE)
    assert prompt.index(COLLABORATION_GUIDANCE) < prompt.index(SUBAGENT_GUIDANCE)
    assert prompt.index(SUBAGENT_GUIDANCE) < prompt.index("REVIEWED-ROLE-GUIDANCE")
    assert prompt.index("REVIEWED-ROLE-GUIDANCE") < prompt.index("VOLATILE-CONTEXT")


def test_collaboration_guidance_resolves_style_conflicts() -> None:
    assert "Be concise by default" in COLLABORATION_GUIDANCE
    assert "when the task's complexity makes that genuinely useful" in COLLABORATION_GUIDANCE
    assert "Do not use corporate jargon" in COLLABORATION_GUIDANCE
    assert "Create productive friction" in COLLABORATION_GUIDANCE
    assert "never humiliate" in COLLABORATION_GUIDANCE
    assert "Do not use sycophancy" in COLLABORATION_GUIDANCE
    assert "likely hallucinations" in COLLABORATION_GUIDANCE
