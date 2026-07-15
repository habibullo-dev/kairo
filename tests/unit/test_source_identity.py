"""Keep current source documentation on the Kira identity.

Legacy names remain only where changing the text would erase migration history, describe an
intentional compatibility boundary, or invalidate the separately reviewed model-prompt baseline.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "jarvis"
LEGACY_PRODUCT_NAME = re.compile(r"\b(?:Kairo|Cairo|Jarvis)\b")

ALLOWED_LEGACY_LINES = {
    (
        "core/prompts.py",
        "You are Jarvis, a precise, capable agentic assistant running on the user's machine.",
    ),
    (
        "persistence/instance_lock.py",
        "Kira acquires the legacy Kairo lock first and the canonical Kira lock second. "
        "Holding both keeps",
    ),
    (
        "persistence/migrations.py",
        "-- --- Artifacts: a first-class, searchable record of things Kairo produced "
        "---------------",
    ),
    (
        "persistence/migrations.py",
        "# holds request_json — Kairo's resolved payload — so execution is byte-faithful "
        "to the approved",
    ),
    (
        "persistence/migrations.py",
        "# equally safe. A genuine v27 Kairo database always has ``tasks``.",
    ),
    (
        "persistence/migrations.py",
        "# A Kairo workstation has exactly one human owner.  Credentials and browser sessions "
        "live in the",
    ),
    (
        "persistence/migrations.py",
        "# until Kairo has a canonical RP host/origin and a maintained WebAuthn verifier.",
    ),
}


def test_source_product_identity_is_kira_except_explicit_legacy_history() -> None:
    found: list[tuple[str, str]] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            if LEGACY_PRODUCT_NAME.search(line):
                found.append((relative, line.strip()))

    assert sorted(found) == sorted(ALLOWED_LEGACY_LINES)


def test_knowledge_front_matter_constant_uses_kira_name() -> None:
    wiki_source = (SOURCE_ROOT / "knowledge" / "wiki.py").read_text(encoding="utf-8")

    assert "JARVIS_KEYS" not in wiki_source
    assert "KIRA_KEYS" in wiki_source
