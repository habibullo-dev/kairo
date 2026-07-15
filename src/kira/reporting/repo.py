"""RepoReader: a hardened, read-only git reader for the Daily "what changed" card.

Git is not just a data source — it *executes* code from repo-local config (fsmonitor hooks,
pagers, ext transports). Reading an untrusted repo naively is an RCE surface. So every
invocation is locked down: an argument list (never a shell), ``GIT_CONFIG_NOSYSTEM=1``, hooks
and fsmonitor and ext-transports disabled via ``-c`` overrides, no pager, no credential prompt,
a pinned cwd, and a hard timeout. Commit subjects and branch names are UNTRUSTED data (the UI
escapes them, the digest frames them). A path without a ``.git`` returns None — never an error.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_TIMEOUT = 5.0
_UNIT = "\x1f"  # ASCII unit separator — safe field delimiter for git --format


@dataclass(frozen=True)
class CommitLine:
    short_rev: str
    when: str  # committer date, ISO-8601
    subject: str  # UNTRUSTED — author-controlled; escape/frame before display


@dataclass(frozen=True)
class RepoState:
    branch: str
    head_rev: str
    dirty_files: int
    recent_commits: list[CommitLine]


def _hardened_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"  # ignore /etc/gitconfig
    env["GIT_TERMINAL_PROMPT"] = "0"  # never block on a credential prompt
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


class RepoReader:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _is_repo(self) -> bool:
        try:
            return self.root.is_dir() and (self.root / ".git").exists()
        except OSError:
            return False

    async def state(self, *, commits: int = 10) -> RepoState | None:
        if not self._is_repo():
            return None
        branch = await self._git("rev-parse", "--abbrev-ref", "HEAD")
        head = await self._git("rev-parse", "--short", "HEAD")
        if branch is None or head is None:
            return None
        dirty = await self._git("status", "--porcelain") or ""
        log = await self._git("log", f"-{max(1, commits)}", f"--format=%h{_UNIT}%cI{_UNIT}%s") or ""
        dirty_files = len([ln for ln in dirty.splitlines() if ln.strip()])
        return RepoState(
            branch=branch.strip(),
            head_rev=head.strip(),
            dirty_files=dirty_files,
            recent_commits=_parse_log(log),
        )

    async def _git(self, *args: str) -> str | None:
        # `-c` overrides neutralise git's own execution surface; --no-pager + argv (no shell).
        cmd = [
            "git",
            "-C",
            str(self.root),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=",  # empty ⇒ no hooks run
            "-c",
            "protocol.ext.allow=never",
            "--no-pager",
            *args,
        ]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                env=_hardened_env(),
                cwd=str(self.root),
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout


def _parse_log(log: str) -> list[CommitLine]:
    commits: list[CommitLine] = []
    for line in log.splitlines():
        if not line.strip():
            continue
        parts = line.split(_UNIT)
        if len(parts) == 3:
            commits.append(CommitLine(short_rev=parts[0], when=parts[1], subject=parts[2]))
    return commits
