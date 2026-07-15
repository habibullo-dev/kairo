"""Contract pins for the current, offline Kira-to-macOS migration ritual."""

from kira.observability.logging import CANONICAL_LOG_PREFIX
from kira.persistence.database_identity import DATABASE_FILENAME, LEGACY_DATABASE_FILENAME
from kira.persistence.migrations import latest_version
from kira.ui.server import STATIC_DIR

REPOSITORY_ROOT = STATIC_DIR.parents[3]
GUIDE = (REPOSITORY_ROOT / "docs" / "migration-macos.md").read_text(encoding="utf-8")
NORMALIZED = " ".join(GUIDE.split())


def test_macos_guide_uses_current_kira_identity_and_cli() -> None:
    for claim in (
        "# Moving Kira from Windows to macOS",
        "uv run kira --ui",
        "uv run kira connect google",
        "uv run kira eval gate",
        f"data/{DATABASE_FILENAME}",
        f"logs/{CANONICAL_LOG_PREFIX}-YYYY-MM-DD.jsonl",
        f"schema v{latest_version()}",
    ):
        assert claim in GUIDE

    for stale_brand in ("Kairo", "Cairo", "Jarvis"):
        assert stale_brand not in GUIDE
    assert "uv run jarvis" not in GUIDE


def test_macos_guide_pins_runtime_and_transfer_preconditions() -> None:
    for claim in (
        "brew install powershell",
        "Kira's shell tool deliberately executes commands through `pwsh`",
        "it is not a default prerequisite",
        "stop every Kira process before copying state",
        "One data root permits one owner process",
        "do not open the same data root from two machines",
        "The migrated database keeps that owner account",
        "a fresh, empty target `data/` path",
        "leave out `data/connectors/`",
        "Do not merge two independently used data roots",
        "uv run kira doctor",
    ):
        assert claim in NORMALIZED

    assert "brew install --cask powershell" not in GUIDE


def test_macos_guide_covers_identity_reset_and_host_specific_paths() -> None:
    for claim in (
        f"legacy `data/{LEGACY_DATABASE_FILENAME}` into `data/{DATABASE_FILENAME}`",
        "leave a small compatibility guard at the legacy path",
        "`.kira-reset-manifests/` and `.data.kira-quarantine-*`",
        "this copy does not include them",
        "`paths.data_dir`, `paths.logs_dir`, `knowledge.dir`",
        "every entry in `connectors.repos`",
        "absolute paths are stored inside the copied database",
        "do not yet relink an existing project's repository list",
    ):
        assert claim in NORMALIZED


def test_macos_guide_keeps_current_auth_backup_and_eval_truth() -> None:
    for claim in (
        "`ANTHROPIC_API_KEY` is required",
        "Restore is not supported",
        "it is **not** the migration source",
        "The committed default is `false`",
        "Bare `kira eval gate` is keyless cassette replay and costs $0",
        "not remote browser access or a cloud wake-up service",
        "a notification chat ID never grants inbound authority",
        "whose port matches `connectors.kakao.redirect_port` must always be registered",
        "Repository CI currently covers Ubuntu and Windows, not macOS",
    ):
        assert claim in NORMALIZED
