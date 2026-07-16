"""Keep current source documentation on the Kira identity.

Legacy names remain only where changing the text would erase migration history or describe an
intentional compatibility boundary.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src" / "kira"
EVAL_SCENARIO_ROOT = ROOT / "tests" / "evals" / "scenarios"
LEGACY_PRODUCT_NAME = re.compile(r"\b(?:Kairo|Cairo|Jarvis)\b")
LEGACY_MODULE_TARGET = re.compile(r"\bjarvis\.(?!db(?:\b|-))")

ALLOWED_LEGACY_LINES = {
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


def test_current_eval_scenarios_use_kira_identity() -> None:
    stale: list[str] = []
    for path in EVAL_SCENARIO_ROOT.rglob("*.yaml"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if LEGACY_PRODUCT_NAME.search(line):
                stale.append(f"{path.relative_to(ROOT).as_posix()}:{number}:{line.strip()}")

    assert stale == []


def test_python_package_namespace_is_kira_only() -> None:
    assert SOURCE_ROOT.is_dir()
    assert not (ROOT / "src" / "jarvis").exists()
    assert importlib.util.find_spec("kira") is not None
    assert importlib.util.find_spec("jarvis") is None


def test_python_imports_and_dynamic_targets_use_kira_namespace() -> None:
    stale_imports: list[str] = []
    stale_targets: list[str] = []
    for base in (SOURCE_ROOT, ROOT / "tests"):
        for path in base.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative = path.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".", 1)[0] == "jarvis":
                            stale_imports.append(f"{relative}:{node.lineno}:{alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split(".", 1)[0] == "jarvis":
                        stale_imports.append(f"{relative}:{node.lineno}:{node.module}")
                elif (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and LEGACY_MODULE_TARGET.search(node.value)
                ):
                    stale_targets.append(f"{relative}:{node.lineno}:{node.value}")

    assert stale_imports == []
    assert stale_targets == []
