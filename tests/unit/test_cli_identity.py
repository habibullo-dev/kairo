"""Canonical Kira command identity with an exact temporary Jarvis alias."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from jarvis import __main__ as entry
from jarvis import __version__

ROOT = Path(__file__).resolve().parents[2]


def test_kira_is_canonical_and_jarvis_is_an_exact_compatibility_alias() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]

    assert scripts["kira"] == "jarvis.__main__:main"
    assert scripts["jarvis"] == scripts["kira"]


@pytest.mark.parametrize("executable", ["kira", "jarvis"])
def test_both_executables_report_the_canonical_kira_version(
    executable: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", [executable, "--version"])

    with pytest.raises(SystemExit) as exited:
        entry.main()

    assert exited.value.code == 0
    assert capsys.readouterr().out.strip() == f"kira {__version__}"


@pytest.mark.parametrize(
    ("argv", "usage"),
    [
        (["kira", "--help"], "usage: kira"),
        (["jarvis", "--help"], "usage: kira"),
        (["kira", "backup", "--help"], "usage: kira backup"),
        (["kira", "connect", "--help"], "usage: kira connect"),
        (["kira", "doctor", "--help"], "usage: kira doctor"),
        (["kira", "dream", "--help"], "usage: kira dream"),
        (["kira", "eval", "--help"], "usage: kira eval"),
        (["kira", "graph", "--help"], "usage: kira graph"),
        (["kira", "reset", "--help"], "usage: kira reset"),
    ],
)
def test_help_uses_the_canonical_kira_command(
    argv: list[str],
    usage: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exited:
        entry.main()

    assert exited.value.code == 0
    assert usage in capsys.readouterr().out
