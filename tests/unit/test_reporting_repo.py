"""RepoReader (Phase 9 Task 8): a hardened, read-only git reader over a scratch repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kira.reporting.repo import RepoReader, _hardened_env, _parse_log

_HAS_GIT = subprocess.run(["git", "--version"], capture_output=True).returncode == 0
requires_git = pytest.mark.skipif(not _HAS_GIT, reason="git not available")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _scratch_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "T")
    (root / "a.txt").write_text("hello", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "first commit")


async def test_none_when_not_a_repo(tmp_path: Path) -> None:
    assert await RepoReader(tmp_path).state() is None  # no .git
    assert await RepoReader(tmp_path / "missing").state() is None


@requires_git
async def test_reads_branch_head_and_commits(tmp_path: Path) -> None:
    _scratch_repo(tmp_path)
    state = await RepoReader(tmp_path).state()
    assert state is not None
    assert state.head_rev and len(state.head_rev) >= 4
    assert state.dirty_files == 0
    assert state.recent_commits and state.recent_commits[0].subject == "first commit"


@requires_git
async def test_dirty_files_counted(tmp_path: Path) -> None:
    _scratch_repo(tmp_path)
    (tmp_path / "b.txt").write_text("new", encoding="utf-8")  # untracked
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")  # modified
    state = await RepoReader(tmp_path).state()
    assert state is not None and state.dirty_files == 2


@requires_git
async def test_commit_subject_is_returned_verbatim_as_data(tmp_path: Path) -> None:
    # A hostile-looking commit subject is returned as plain data (the UI escapes it).
    _scratch_repo(tmp_path)
    (tmp_path / "c.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "c.txt")
    _git(tmp_path, "commit", "-q", "-m", "<img src=x> & pipes | ; rm -rf")
    state = await RepoReader(tmp_path).state()
    assert state.recent_commits[0].subject == "<img src=x> & pipes | ; rm -rf"


def test_hardened_env_disables_system_config() -> None:
    env = _hardened_env()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_parse_log_tolerates_malformed_lines() -> None:
    log = "abc123\x1f2026-01-01T00:00:00\x1ffix bug\n\ngarbage-line\n"
    commits = _parse_log(log)
    assert len(commits) == 1 and commits[0].subject == "fix bug"
