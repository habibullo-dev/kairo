"""Shell tool: run a PowerShell 7 (pwsh) command.

Defaults to ``ask`` and is refined by the gate's shell prefix rules — the model
can request any command, but the human (or policy) decides. Output is capped and
the process is killed on timeout so a hung command can't wedge the loop.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from kira.tools.base import Permission, Tool, ToolResult

_MAX_OUTPUT_CHARS = 40_000


class RunShellParams(BaseModel):
    command: str = Field(description="PowerShell command to run.")
    cwd: str | None = Field(default=None, description="Working directory (optional).")
    timeout_seconds: float = Field(default=60.0, description="Kill the command after this long.")


class RunShellTool(Tool):
    name = "run_shell"
    description = (
        "Run a PowerShell 7 (pwsh) command and return its stdout/stderr and exit code. "
        "Non-interactive; the command cannot prompt for input."
    )
    Params = RunShellParams
    permission_default = Permission.ASK

    async def run(self, params: RunShellParams) -> ToolResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                params.command,
                cwd=params.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return ToolResult(content="pwsh (PowerShell 7) is not installed.", is_error=True)

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=params.timeout_seconds
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(
                content=f"Command timed out after {params.timeout_seconds:g}s.", is_error=True
            )

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        parts = [f"[exit {proc.returncode}]"]
        if out.strip():
            parts.append(out.rstrip())
        if err.strip():
            parts.append(f"[stderr]\n{err.rstrip()}")
        text = "\n".join(parts)[:_MAX_OUTPUT_CHARS]
        return ToolResult(content=text, is_error=proc.returncode != 0)
