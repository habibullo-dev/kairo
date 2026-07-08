"""Provider smoke bench (E3): replay fail-closed + live skips unavailable providers."""

from __future__ import annotations

from pathlib import Path

from tests.evals.cassette import CassetteConfig
from tests.evals.runner import run_smoke

from jarvis.config import load_config


def _cfg(tmp_path: Path):
    return load_config(root=tmp_path, env_file=None)


async def test_smoke_replay_fails_closed_without_cassettes(tmp_path: Path) -> None:
    # Replay + no cassettes ⇒ every tiny scenario is a fail-closed miss (no live call, keyless).
    cc = CassetteConfig(mode="replay", store_dir=tmp_path / "cass", max_cost_usd=None)
    rc = await run_smoke(_cfg(tmp_path), providers=["anthropic"], cassette_cfg=cc, runs=1)
    assert rc == 1  # 2 smoke scenarios both missing ⇒ failures


async def test_smoke_live_skips_unavailable_provider(tmp_path: Path) -> None:
    # Z.ai has no key + is not enabled ⇒ live smoke skips it fail-closed (attempted=0 ⇒ rc 0).
    cc = CassetteConfig(mode="live", store_dir=tmp_path / "cass", max_cost_usd=3.0)
    rc = await run_smoke(_cfg(tmp_path), providers=["zai"], cassette_cfg=cc, runs=1)
    assert rc == 0  # nothing attempted (skipped), so no failures — never a live call


async def test_smoke_unknown_provider_is_skipped(tmp_path: Path) -> None:
    cc = CassetteConfig(mode="replay", store_dir=tmp_path / "cass", max_cost_usd=None)
    rc = await run_smoke(_cfg(tmp_path), providers=["nope"], cassette_cfg=cc, runs=1)
    assert rc == 0  # unknown provider skipped, nothing attempted
