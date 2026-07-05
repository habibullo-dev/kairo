"""Tool framework tests: schema generation, registry, discovery, executor guards."""

from __future__ import annotations

import asyncio
from types import ModuleType

import pytest
from pydantic import BaseModel

from jarvis.tools import Permission, Tool, ToolExecutor, ToolRegistry, ToolResult


class EchoParams(BaseModel):
    text: str
    times: int = 1


class EchoTool(Tool):
    name = "echo"
    description = "Echo text back, optionally repeated."
    Params = EchoParams
    permission_default = Permission.ALLOW

    async def run(self, params: EchoParams) -> str:
        return params.text * params.times


class EmptyParams(BaseModel):
    pass


class SlowTool(Tool):
    name = "slow"
    description = "Sleeps forever."
    Params = EmptyParams

    async def run(self, params: EmptyParams) -> str:
        await asyncio.sleep(30)
        return "done"


class BoomTool(Tool):
    name = "boom"
    description = "Always raises."
    Params = EmptyParams

    async def run(self, params: EmptyParams) -> str:
        raise RuntimeError("kaboom")


class BigParams(BaseModel):
    n: int
    is_error: bool = False


class BigTool(Tool):
    name = "big"
    description = "Returns a large payload."
    Params = BigParams

    async def run(self, params: BigParams) -> ToolResult:
        return ToolResult(content="x" * params.n, is_error=params.is_error)


# --- Tool contract / schema ------------------------------------------------


def test_input_schema_reflects_params() -> None:
    schema = EchoTool().input_schema()
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"text", "times"}
    assert schema["required"] == ["text"]  # `times` has a default


def test_tool_spec_shape() -> None:
    spec = EchoTool().tool_spec()
    assert spec["name"] == "echo"
    assert spec["description"].startswith("Echo")
    assert spec["input_schema"]["properties"]["times"]["default"] == 1


def test_concrete_subclass_must_declare_attributes() -> None:
    with pytest.raises(TypeError):

        class Bad(Tool):  # missing name/description/Params, but run is defined
            async def run(self, params: BaseModel) -> str:
                return "x"


# --- Registry --------------------------------------------------------------


def test_register_get_contains_len() -> None:
    reg = ToolRegistry()
    reg.register(EchoTool())
    assert "echo" in reg
    assert len(reg) == 1
    assert isinstance(reg.get("echo"), EchoTool)
    assert reg.get("missing") is None
    assert reg.names() == ["echo"]


def test_duplicate_registration_raises() -> None:
    reg = ToolRegistry()
    reg.register(EchoTool())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(EchoTool())


def test_specs_returns_one_per_tool() -> None:
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(BigTool())
    specs = reg.specs()
    assert {s["name"] for s in specs} == {"echo", "big"}


def test_register_from_module_finds_defined_tools_only() -> None:
    # A synthetic module that "defines" EchoTool and imports the base Tool.
    mod = ModuleType("fake_builtin")
    local_echo = type("LocalEcho", (EchoTool,), {"name": "local_echo"})
    local_echo.__module__ = "fake_builtin"
    mod.LocalEcho = local_echo
    mod.Tool = Tool  # imported base — must be ignored
    mod.EchoTool = EchoTool  # imported (defined elsewhere) — must be ignored

    reg = ToolRegistry()
    count = reg.register_from_module(mod)
    assert count == 1
    assert reg.names() == ["local_echo"]


def test_discover_empty_builtin_package() -> None:
    reg = ToolRegistry()
    # builtin is empty until task 9 — discovery must succeed and register nothing.
    assert reg.discover("jarvis.tools.builtin") == 0


# --- Executor --------------------------------------------------------------


async def test_execute_success_from_str() -> None:
    ex = ToolExecutor()
    result = await ex.execute(EchoTool(), {"text": "hi", "times": 3})
    assert result.content == "hihihi"
    assert result.is_error is False


async def test_execute_validation_error_becomes_result() -> None:
    ex = ToolExecutor()
    result = await ex.execute(EchoTool(), {})  # missing required `text`
    assert result.is_error is True
    assert "Invalid input" in result.content


async def test_execute_timeout_becomes_result() -> None:
    ex = ToolExecutor(timeout=0.05)
    result = await ex.execute(SlowTool(), {})
    assert result.is_error is True
    assert "timed out" in result.content


async def test_execute_exception_becomes_result() -> None:
    ex = ToolExecutor()
    result = await ex.execute(BoomTool(), {})
    assert result.is_error is True
    assert "RuntimeError" in result.content and "kaboom" in result.content


async def test_execute_truncates_long_output() -> None:
    ex = ToolExecutor(max_result_chars=100)
    result = await ex.execute(BigTool(), {"n": 5000})
    assert result.content.startswith("x" * 100)
    assert "truncated" in result.content
    assert len(result.content) < 5000


async def test_truncation_preserves_is_error() -> None:
    ex = ToolExecutor(max_result_chars=50)
    result = await ex.execute(BigTool(), {"n": 5000, "is_error": True})
    assert result.is_error is True
    assert "truncated" in result.content


async def test_short_output_not_truncated() -> None:
    ex = ToolExecutor(max_result_chars=1000)
    result = await ex.execute(BigTool(), {"n": 10})
    assert result.content == "x" * 10
    assert "truncated" not in result.content
