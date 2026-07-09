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


def _loop(tmp_path: Path, client, *, model_override=None, effort_override=None):
    cfg = load_config(root=tmp_path, env_file=None)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=cfg))
    loop = AgentLoop(
        client=client, registry=reg, executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path), config=cfg, system=build_system(),
        model_override=model_override, effort_override=effort_override,
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


# --- per-model effort (cost control) ---------------------------------------
def test_effort_is_per_model_validated_and_defaulted() -> None:
    st = InteractiveModelState("claude-sonnet-5", default_effort="high")
    assert st.current_effort() == "high"  # unset ⇒ default
    st.set_effort("low")  # sets the CURRENT model (sonnet)
    assert st.current_effort() == "low"
    st.set("claude-opus-4-8")
    assert st.current_effort() == "high"  # opus has its own (default) effort — per model
    st.set("claude-sonnet-5")
    assert st.current_effort() == "low"  # sonnet's choice was remembered
    st.set_effort("max", model_id="claude-opus-4-8")  # set another model explicitly
    assert st.efforts()["claude-opus-4-8"] == "max"
    with pytest.raises(ValueError):
        st.set_effort("turbo")  # not a valid effort level
    with pytest.raises(ValueError):
        st.set_effort("low", model_id="gpt-5.2")  # not an allowed (anthropic) model
    assert st.current_effort() == "low"  # a rejected set never changes state


async def test_loop_uses_effort_override_frozen_per_turn(tmp_path: Path) -> None:
    st = InteractiveModelState("claude-sonnet-5", default_effort="high")
    st.set_effort("low")
    client = FakeClient([text_message("one"), text_message("two")])
    loop, _cfg = _loop(tmp_path, client, effort_override=st.current_effort)
    await loop.run_turn([{"role": "user", "content": "a"}])
    assert client.calls[-1]["effort"] == "low"
    st.set_effort("max")
    await loop.run_turn([{"role": "user", "content": "b"}])
    assert client.calls[-1]["effort"] == "max"  # the switch applied on the NEXT turn


async def test_no_effort_override_sends_no_effort(tmp_path: Path) -> None:
    # Unset ⇒ the loop omits the effort kwarg ⇒ the client applies its configured default
    # (byte-identical to a build without the selector). FakeClient records None.
    client = FakeClient([text_message("hi")])
    loop, _cfg = _loop(tmp_path, client, effort_override=None)
    await loop.run_turn([{"role": "user", "content": "x"}])
    assert client.calls[-1]["effort"] is None


async def test_override_returning_falsy_falls_back_to_config_default(tmp_path: Path) -> None:
    client = FakeClient([text_message("hi")])
    loop, cfg = _loop(tmp_path, client, model_override=lambda: None)
    await loop.run_turn([{"role": "user", "content": "x"}])
    assert client.calls[-1]["model"] == cfg.models.main


def test_interactive_models_resilient_to_provider_failure(tmp_path: Path, monkeypatch) -> None:
    # A pricing/provider hiccup must NEVER empty the model picker (the Checkpoint-J2 blocker-1 root
    # cause: a throwing /api/models → an empty <select>). The four Anthropic models are always
    # listed + selectable; only the external-provider states degrade.
    import jarvis.models.providers as prov
    from jarvis.ui.readmodels import interactive_models

    def boom(*_a, **_k):
        raise RuntimeError("pricing table exploded")

    monkeypatch.setattr(prov.ProviderRegistry, "from_config", classmethod(boom))
    m = interactive_models(load_config(root=tmp_path, env_file=None))
    assert len(m["models"]) == 4 and all(x["selectable"] for x in m["models"])
    assert m["external"] == []  # degraded, but the picker is never empty
