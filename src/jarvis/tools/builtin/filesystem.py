"""Filesystem tools: read, write, list, glob.

Reads/lists/globs default to ``allow`` (safe, reversible); writes default to
``ask`` and are additionally gated by the PermissionGate's write allowlist.

Two safety properties are load-bearing here:

* **Unified resolution.** Relative paths resolve against the workspace root
  (``config.root``) via :func:`jarvis.paths.resolve_path` — the *same* resolution
  the PermissionGate uses. Without this, the gate could approve one file while the
  tool touched another (gate uses the root; a naive tool would use the CWD).
* **Bounded reads.** ``read_file`` reads at most a hard ceiling of bytes *from
  disk* (``min(max_bytes, limits.max_read_bytes)``), so a multi-gigabyte file
  can't be slurped into memory even if the model asks for it. ``list_dir`` and
  ``glob_search`` cap how much they return.

Each tool's blocking work lives in a module-level sync helper that ``run`` calls
via ``asyncio.to_thread`` — so filesystem I/O never stalls the event loop while
other tools run in parallel, and the sync helpers are directly unit-testable.
The ``_root`` / ``_limit`` helpers are module-level (not a shared base class) on
purpose: an intermediate ``Tool`` subclass would trip ``Tool.__init_subclass__``'s
required-attribute check at import time.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from jarvis.paths import is_sensitive_path, resolve_path
from jarvis.tools.base import Permission, Tool, ToolContext, ToolResult

# Fallbacks used only when a tool has no injected config (e.g. bare unit tests);
# the real limits come from config.limits.
_MAX_READ_BYTES = 1_000_000
_MAX_DIR_ENTRIES = 1_000


def _root(context: ToolContext) -> Path:
    """Workspace root to resolve relative paths against (matches the gate)."""
    cfg = getattr(context, "config", None)
    return cfg.root if cfg is not None else Path.cwd()


def _limit(context: ToolContext, name: str, default: int) -> int:
    """A ``config.limits`` value, or a fallback when no config is injected."""
    cfg = getattr(context, "config", None)
    return getattr(cfg.limits, name) if cfg is not None else default


class ReadFileParams(BaseModel):
    path: str = Field(description="Path to the file to read.")
    max_bytes: int = Field(default=200_000, description="Cap on bytes read (clamped to a limit).")


def _read_file(path: Path, cap: int) -> ToolResult | str:
    if not path.exists():
        return ToolResult(content=f"No such file: {path}", is_error=True)
    if path.is_dir():
        return ToolResult(content=f"{path} is a directory; use list_dir.", is_error=True)
    # Read at most cap+1 bytes so we can detect truncation without loading the
    # whole file — a 5 GB file costs one buffer of `cap` bytes, not 5 GB of RAM.
    with path.open("rb") as fh:
        raw = fh.read(cap + 1)
    text = raw[:cap].decode("utf-8", errors="replace")
    if len(raw) > cap:
        text += f"\n\n[... file truncated at {cap} bytes ...]"
    return text


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file and return its contents."
    Params = ReadFileParams
    permission_default = Permission.ALLOW

    async def run(self, params: ReadFileParams) -> ToolResult | str:
        cap = max(0, min(params.max_bytes, _limit(self.context, "max_read_bytes", _MAX_READ_BYTES)))
        target = resolve_path(params.path, _root(self.context))
        return await asyncio.to_thread(_read_file, target, cap)


class WriteFileParams(BaseModel):
    path: str = Field(description="Path to write (parent dirs are created).")
    content: str = Field(description="Full contents to write (overwrites).")


def _write_file(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {path}."


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write (overwrite) a UTF-8 text file, creating parent directories."
    Params = WriteFileParams
    permission_default = Permission.ASK

    async def run(self, params: WriteFileParams) -> str:
        target = resolve_path(params.path, _root(self.context))
        return await asyncio.to_thread(_write_file, target, params.content)


class ListDirParams(BaseModel):
    path: str = Field(default=".", description="Directory to list.")


def _list_dir(path: Path, max_entries: int) -> ToolResult | str:
    if not path.exists():
        return ToolResult(content=f"No such directory: {path}", is_error=True)
    if not path.is_dir():
        return ToolResult(content=f"{path} is not a directory.", is_error=True)
    # Redact sensitive entries (Phase 9): the connector token file, .env, SSH keys, etc. must
    # not be revealed even by name — the sensitive-path floor now spans list/glob, not just
    # read_file. Filtered before slicing so the "N more" count reflects visible entries only.
    entries = sorted(
        (e for e in path.iterdir() if not is_sensitive_path(e)),
        key=lambda e: (e.is_file(), e.name.lower()),
    )
    shown = entries[:max_entries]
    lines = [f"{'d' if e.is_dir() else 'f'}  {e.name}" for e in shown]
    body = "\n".join(lines) or "(empty directory)"
    if len(entries) > len(shown):
        body += f"\n[... {len(entries) - len(shown)} more entries omitted ...]"
    return body


class ListDirTool(Tool):
    name = "list_dir"
    description = "List the entries of a directory (directories first)."
    Params = ListDirParams
    permission_default = Permission.ALLOW

    async def run(self, params: ListDirParams) -> ToolResult | str:
        target = resolve_path(params.path, _root(self.context))
        max_entries = _limit(self.context, "max_dir_entries", _MAX_DIR_ENTRIES)
        return await asyncio.to_thread(_list_dir, target, max_entries)


class GlobParams(BaseModel):
    pattern: str = Field(description="Glob pattern, e.g. '**/*.py'.")
    root: str = Field(default=".", description="Directory to search from.")
    max_results: int = Field(default=200, description="Cap on matches returned (clamped).")


def _glob_search(root: Path, pattern: str, max_results: int) -> ToolResult | str:
    if not root.is_dir():
        return ToolResult(content=f"No such directory: {root}", is_error=True)
    # Redact sensitive matches (Phase 9) — a glob must not surface the connector token path.
    matches = sorted(str(p) for p in root.glob(pattern) if not is_sensitive_path(p))
    shown = matches[:max_results]
    header = f"{len(matches)} match(es)"
    if len(matches) > len(shown):
        header += f" (showing first {len(shown)})"
    body = "\n".join(shown) if shown else "(no matches)"
    return f"{header}:\n{body}"


class GlobSearchTool(Tool):
    name = "glob_search"
    description = "Find files matching a glob pattern under a root directory."
    Params = GlobParams
    permission_default = Permission.ALLOW

    async def run(self, params: GlobParams) -> ToolResult | str:
        target = resolve_path(params.root, _root(self.context))
        ceiling = _limit(self.context, "max_dir_entries", _MAX_DIR_ENTRIES)
        cap = max(0, min(params.max_results, ceiling))
        return await asyncio.to_thread(_glob_search, target, params.pattern, cap)
