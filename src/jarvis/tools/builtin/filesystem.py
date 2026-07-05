"""Filesystem tools: read, write, list, glob.

Reads/lists/globs default to ``allow`` (safe, reversible); writes default to
``ask`` and are additionally gated by the PermissionGate's write allowlist.

Each tool's blocking work lives in a module-level sync helper that ``run`` calls
via ``asyncio.to_thread`` — so filesystem I/O never stalls the event loop while
other tools run in parallel, and the sync helpers are also directly unit-testable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from jarvis.tools.base import Permission, Tool, ToolResult


class ReadFileParams(BaseModel):
    path: str = Field(description="Path to the file to read.")
    max_bytes: int = Field(default=200_000, description="Cap on bytes read.")


def _read_file(params: ReadFileParams) -> ToolResult | str:
    path = Path(params.path)
    if not path.exists():
        return ToolResult(content=f"No such file: {path}", is_error=True)
    if path.is_dir():
        return ToolResult(content=f"{path} is a directory; use list_dir.", is_error=True)
    raw = path.read_bytes()
    text = raw[: params.max_bytes].decode("utf-8", errors="replace")
    if len(raw) > params.max_bytes:
        text += f"\n\n[... file truncated at {params.max_bytes} bytes ...]"
    return text


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file and return its contents."
    Params = ReadFileParams
    permission_default = Permission.ALLOW

    async def run(self, params: ReadFileParams) -> ToolResult | str:
        return await asyncio.to_thread(_read_file, params)


class WriteFileParams(BaseModel):
    path: str = Field(description="Path to write (parent dirs are created).")
    content: str = Field(description="Full contents to write (overwrites).")


def _write_file(params: WriteFileParams) -> str:
    path = Path(params.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(params.content, encoding="utf-8")
    return f"Wrote {len(params.content)} characters to {path}."


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write (overwrite) a UTF-8 text file, creating parent directories."
    Params = WriteFileParams
    permission_default = Permission.ASK

    async def run(self, params: WriteFileParams) -> str:
        return await asyncio.to_thread(_write_file, params)


class ListDirParams(BaseModel):
    path: str = Field(default=".", description="Directory to list.")


def _list_dir(params: ListDirParams) -> ToolResult | str:
    path = Path(params.path)
    if not path.exists():
        return ToolResult(content=f"No such directory: {path}", is_error=True)
    if not path.is_dir():
        return ToolResult(content=f"{path} is not a directory.", is_error=True)
    entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    lines = [f"{'d' if e.is_dir() else 'f'}  {e.name}" for e in entries]
    return "\n".join(lines) or "(empty directory)"


class ListDirTool(Tool):
    name = "list_dir"
    description = "List the entries of a directory (directories first)."
    Params = ListDirParams
    permission_default = Permission.ALLOW

    async def run(self, params: ListDirParams) -> ToolResult | str:
        return await asyncio.to_thread(_list_dir, params)


class GlobParams(BaseModel):
    pattern: str = Field(description="Glob pattern, e.g. '**/*.py'.")
    root: str = Field(default=".", description="Directory to search from.")
    max_results: int = Field(default=200, description="Cap on matches returned.")


def _glob_search(params: GlobParams) -> ToolResult | str:
    root = Path(params.root)
    if not root.is_dir():
        return ToolResult(content=f"No such directory: {root}", is_error=True)
    matches = sorted(str(p) for p in root.glob(params.pattern))
    shown = matches[: params.max_results]
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
        return await asyncio.to_thread(_glob_search, params)
