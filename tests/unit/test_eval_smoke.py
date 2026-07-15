"""Provider smoke bench: unavailable defaults may skip, but liveness never passes vacuously."""

from __future__ import annotations

from pathlib import Path

from tests.evals.cassette import CassetteConfig
from tests.evals.runner import REPO_ROOT, run_smoke

from kira.config import load_config
from kira.core import FakeClient, text_message


def _cfg(tmp_path: Path):
    return load_config(root=tmp_path, env_file=None)


async def test_smoke_replay_fails_closed_without_cassettes(tmp_path: Path) -> None:
    # Replay + no cassettes ⇒ every tiny scenario is a fail-closed miss (no live call, keyless).
    cc = CassetteConfig(mode="replay", store_dir=tmp_path / "cass", max_cost_usd=None)
    rc = await run_smoke(_cfg(tmp_path), providers=["anthropic"], cassette_cfg=cc, runs=1)
    assert rc == 1  # 2 smoke scenarios both missing ⇒ failures


async def test_smoke_explicit_unavailable_provider_fails(tmp_path: Path, capsys) -> None:
    # Z.ai has no key + is not enabled: an explicit liveness request cannot pass by skipping it.
    cc = CassetteConfig(mode="live", store_dir=tmp_path / "cass", max_cost_usd=3.0)
    rc = await run_smoke(
        _cfg(tmp_path),
        providers=["zai"],
        cassette_cfg=cc,
        runs=1,
        required_providers=frozenset({"zai"}),
    )
    assert rc == 1
    output = capsys.readouterr().out
    assert "attempted=0" in output
    assert "unmet_requested=['zai']" in output


async def test_smoke_zero_attempts_fails_even_when_skips_are_optional(
    tmp_path: Path, capsys
) -> None:
    cc = CassetteConfig(mode="replay", store_dir=tmp_path / "cass", max_cost_usd=None)
    rc = await run_smoke(_cfg(tmp_path), providers=["nope"], cassette_cfg=cc, runs=1)
    assert rc == 1
    output = capsys.readouterr().out
    assert "attempted=0" in output
    assert "FAIL: no provider call was attempted" in output


async def test_default_smoke_allows_optional_skip_when_another_provider_passes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from kira.models.factory import ClientFactory

    config = load_config(root=REPO_ROOT, env_file=None)
    secrets = config.secrets.model_copy(update={"anthropic_api_key": "test-key"})
    providers = config.providers.model_copy(update={"enabled": []})
    config = config.model_copy(update={"secrets": secrets, "providers": providers})
    client = FakeClient([text_message("4"), text_message("OK")])
    monkeypatch.setattr(ClientFactory, "for_route", lambda _self, _route: client)
    cc = CassetteConfig(mode="live", store_dir=tmp_path / "cass", max_cost_usd=3.0)

    rc = await run_smoke(
        config,
        providers=["zai", "anthropic"],
        cassette_cfg=cc,
        runs=1,
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "zai: disabled — skip" in output
    assert "attempted=2 failures=0 unmet_requested=[]" in output
