"""Keep legacy product wording confined to documented history or compatibility."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY_NAME = re.compile(r"\b(?:Kairo|Cairo|Jarvis)\b", re.IGNORECASE)
MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)#]+)(?:#[^)]*)?\)")
URI_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
HISTORICAL_BANNER = "**Historical design record.**"

CURRENT_COMPATIBILITY_LINES = {
    "README.md": (
        "The exact `jarvis` command remains temporarily available",
        "legacy `data/jarvis.db`",
    ),
    "docs/architecture.md": (
        "default `data/jarvis.db`",
        "Legacy `jarvis-YYYY-MM-DD.jsonl`",
    ),
    "docs/migration-macos.md": ("`data/jarvis.db` into `data/kira.db`",),
}

BANNERED_ENTRY_POINTS = (
    "docs/PLAN.md",
    "docs/ROADMAP-12-16-execution.md",
    "docs/ROADMAP-post-10B.md",
    "docs/learning-notes.md",
    "docs/phase-11-implementation-playbook.md",
    "docs/fable-frontend-parity/00-executive-summary.md",
    "docs/fable-skill-forge/00-README.md",
)


def _is_historical(relative: str) -> bool:
    path = Path(relative)
    name = path.name
    return (
        relative.startswith("docs/decisions/")
        or relative.startswith("docs/fable-")
        or name.startswith(("PLAN", "ROADMAP", "verification-", "evals-baseline"))
        or relative
        in {
            "docs/KIRA_10X_PRODUCT_PLATFORM_PLAN.md",
            "docs/learning-notes.md",
            "docs/phase-11-implementation-playbook.md",
        }
    )


def test_legacy_documentation_names_are_classified() -> None:
    unexpected: list[str] = []
    files = [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not LEGACY_NAME.search(line) or _is_historical(relative):
                continue
            allowed = CURRENT_COMPATIBILITY_LINES.get(relative, ())
            if not any(fragment in line for fragment in allowed):
                unexpected.append(f"{relative}:{number}:{line.strip()}")

    assert unexpected == []


def test_ambiguous_historical_entry_points_are_bannered() -> None:
    for relative in BANNERED_ENTRY_POINTS:
        opening = "\n".join((ROOT / relative).read_text(encoding="utf-8").splitlines()[:10])
        assert HISTORICAL_BANNER in opening, relative


def test_current_documentation_index_links_resolve() -> None:
    for relative in ("README.md", "docs/README.md"):
        path = ROOT / relative
        for target in MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
            if URI_SCHEME.match(target):
                continue
            resolved = (path.parent / target).resolve()
            assert resolved.exists(), f"broken documentation link: {relative} -> {target}"
