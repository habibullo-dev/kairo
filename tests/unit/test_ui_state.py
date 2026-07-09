"""Interactive model state + the loop's model_override seam (Phase 15.5).

The chat model is switchable at runtime, but only within the Anthropic allowlist (the main chat
carries private context; 10C pins private_ok to anthropic). The loop reads the choice FROZEN per
turn (a switch applies next turn), and NO override is byte-identical to config.models.main — the
seam is invisible until a human picks another model. Keyless via FakeClient."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.config import load_config
from jarvis.core import AgentLoop, FakeClient, build_system, text_message
from jarvis.permissions import PermissionGate, Policy
from jarvis.tools import ToolContext, ToolExecutor, ToolRegistry
from jarvis.ui.state import ALLOWED_MODEL_IDS, INTERACTIVE_MODELS, InteractiveModelState


def _loop(tmp_path: Path, client, *, model_override=None):
    cfg = load_config(root=tmp_path, env_file=None)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    loop = AgentLoop(
        client=client, registry=reg, executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path), config=cfg, system=build_system(),
        model_override=model_override,
    )
    return loop, cfg


# --- allowlist: Anthropic-only, fail-closed --------------------------------
def test_state_switches_within_allowlist_and_rejects_others() -> None:
    st = InteractiveModelState("claude-opus-4-8")
    assert st.current() == "claude-opus-4-8"
    st.set("claude-sonnet-5")
    assert st.current() == "claude-sonnet-5"
    with pytest.raises(ValueError):
        st.set("gpt-5.2")  # an external provider — not private_ok (10C)
    with pytest.raises(ValueError):
        st.set("claude-not-real")  # unknown id
    assert st.current() == "claude-sonnet-5"  # a rejected set never changes the state


def test_allowlist_is_anthropic_only() -> None:
    assert {mid for mid, _ in INTERACTIVE_MODELS} == ALLOWED_MODEL_IDS
    assert all(mid.startswith("claude-") for mid in ALLOWED_MODEL_IDS)


# --- the loop seam ---------------------------------------------------------
async def test_loop_uses_override_model_frozen_per_turn(tmp_path: Path) -> None:
    st = InteractiveModelState("claude-opus-4-8")
    client = FakeClient([text_message("one"), text_message("two")])
    loop, _cfg = _loop(tmp_path, client, model_override=st.current)
    await loop.run_turn([{"role": "user", "content": "a"}])
    assert client.calls[-1]["model"] == "claude-opus-4-8"
    st.set("claude-sonnet-5")
    await loop.run_turn([{"role": "user", "content": "b"}])
    assert client.calls[-1]["model"] == "claude-sonnet-5"  # the switch applied on the NEXT turn


async def test_no_override_is_byte_identical_to_config_default(tmp_path: Path) -> None:
    client = FakeClient([text_message("hi")])
    loop, cfg = _loop(tmp_path, client, model_override=None)
    await loop.run_turn([{"role": "user", "content": "x"}])
    assert client.calls[-1]["model"] == cfg.models.main


async def test_override_returning_falsy_falls_back_to_config_default(tmp_path: Path) -> None:
    client = FakeClient([text_message("hi")])
    loop, cfg = _loop(tmp_path, client, model_override=lambda: None)
    await loop.run_turn([{"role": "user", "content": "x"}])
    assert client.calls[-1]["model"] == cfg.models.main
