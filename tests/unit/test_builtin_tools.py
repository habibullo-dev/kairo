"""Tests for built-in tools: filesystem + shell run for real, web is mocked."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from jarvis.config import Config, LimitsConfig, ModelsConfig, PathsConfig, Secrets, load_config
from jarvis.paths import resolve_path
from jarvis.tools import Permission, ToolContext, ToolResult
from jarvis.tools.builtin import web
from jarvis.tools.builtin.filesystem import (
    GlobSearchTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from jarvis.tools.builtin.shell import RunShellTool
from jarvis.tools.builtin.web import WebFetchTool, WebSearchTool

pwsh = shutil.which("pwsh")


def content_of(r: object) -> str:
    return r.content if isinstance(r, ToolResult) else str(r)


def is_error(r: object) -> bool:
    return isinstance(r, ToolResult) and r.is_error


def test_tool_context_optional_services_default_to_none() -> None:
    ctx = ToolContext()
    assert ctx.config is None
    assert ctx.memory is None
    assert ctx.tasks is None  # scheduler disabled ⇒ task tools won't register
    assert ctx.knowledge is None  # knowledge base disabled ⇒ kb tools won't register


# --- filesystem ------------------------------------------------------------


async def test_read_file(tmp_path: Path) -> None:
    f = tmp_path / "note.txt"
    f.write_text("hello world", encoding="utf-8")
    result = await ReadFileTool().run(ReadFileTool.Params(path=str(f)))
    assert content_of(result) == "hello world"


async def test_list_dir_redacts_sensitive_entries(tmp_path: Path) -> None:
    # Phase 9 cross-cutting floor: a secret must not be revealed even by name in a listing.
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ok", encoding="utf-8")
    body = content_of(await ListDirTool().run(ListDirTool.Params(path=str(tmp_path))))
    assert "notes.txt" in body
    assert ".env" not in body


async def test_glob_search_redacts_sensitive_matches(tmp_path: Path) -> None:
    d = tmp_path / "data" / "connectors"
    d.mkdir(parents=True)
    (d / "google_token.json").write_text("{}", encoding="utf-8")
    (tmp_path / "keep.json").write_text("{}", encoding="utf-8")
    body = content_of(
        await GlobSearchTool().run(GlobSearchTool.Params(pattern="**/*.json", root=str(tmp_path)))
    )
    assert "keep.json" in body
    assert "google_token.json" not in body


async def test_read_missing_file_is_error(tmp_path: Path) -> None:
    result = await ReadFileTool().run(ReadFileTool.Params(path=str(tmp_path / "nope.txt")))
    assert is_error(result)


async def test_read_directory_is_error(tmp_path: Path) -> None:
    result = await ReadFileTool().run(ReadFileTool.Params(path=str(tmp_path)))
    assert is_error(result)


async def test_read_truncates_at_max_bytes(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("x" * 1000, encoding="utf-8")
    result = await ReadFileTool().run(ReadFileTool.Params(path=str(f), max_bytes=100))
    text = content_of(result)
    assert text.startswith("x" * 100)
    assert "truncated" in text


async def test_write_file_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "dir" / "out.txt"
    result = await WriteFileTool().run(WriteFileTool.Params(path=str(target), content="data"))
    assert "Wrote" in content_of(result)
    assert target.read_text(encoding="utf-8") == "data"


async def test_list_dir(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("", encoding="utf-8")
    (tmp_path / "adir").mkdir()
    result = content_of(await ListDirTool().run(ListDirTool.Params(path=str(tmp_path))))
    # directories first
    assert result.splitlines()[0].startswith("d")
    assert "adir" in result and "b.txt" in result


async def test_list_missing_dir_is_error(tmp_path: Path) -> None:
    result = await ListDirTool().run(ListDirTool.Params(path=str(tmp_path / "nope")))
    assert is_error(result)


async def test_glob_search(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "c.txt").write_text("", encoding="utf-8")
    result = content_of(
        await GlobSearchTool().run(GlobSearchTool.Params(pattern="*.py", root=str(tmp_path)))
    )
    assert "2 match" in result
    assert "a.py" in result and "b.py" in result and "c.txt" not in result


# --- hardening: bounded reads + unified resolution -------------------------


def _ctx_with_root(root: Path) -> ToolContext:
    return ToolContext(config=load_config(root=root, env_file=None))


async def test_read_hard_ceiling_clamps_oversized_request(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.limits.max_read_bytes = 10  # tighten the ceiling for the test
    f = tmp_path / "big.txt"
    f.write_text("y" * 500, encoding="utf-8")
    tool = ReadFileTool(ToolContext(config=cfg))
    # model asks for a 10 MB read; the ceiling wins regardless
    out = content_of(await tool.run(ReadFileTool.Params(path=str(f), max_bytes=10_000_000)))
    assert out.startswith("y" * 10)
    assert "truncated at 10 bytes" in out


async def test_list_dir_caps_entries(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.limits.max_dir_entries = 3
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("", encoding="utf-8")
    out = content_of(await ListDirTool(ToolContext(config=cfg)).run(ListDirTool.Params(path=".")))
    assert "more entries omitted" in out


async def test_glob_clamps_to_ceiling(tmp_path: Path) -> None:
    cfg = load_config(root=tmp_path, env_file=None)
    cfg.limits.max_dir_entries = 2
    for i in range(5):
        (tmp_path / f"a{i}.py").write_text("", encoding="utf-8")
    tool = GlobSearchTool(ToolContext(config=cfg))
    out = content_of(
        await tool.run(GlobSearchTool.Params(pattern="*.py", root=".", max_results=100))
    )
    assert "showing first 2" in out


async def test_write_resolves_relative_against_config_root(tmp_path: Path) -> None:
    # The tool must resolve a relative path against config.root (like the gate),
    # NOT the process CWD — otherwise the gate could approve a different file.
    cfg = load_config(root=tmp_path, env_file=None)
    await WriteFileTool(ToolContext(config=cfg)).run(
        WriteFileTool.Params(path="notes/out.txt", content="hi")
    )
    written = tmp_path / "notes" / "out.txt"
    assert written.read_text(encoding="utf-8") == "hi"
    # identical resolution to what the gate would compute for the same input
    assert resolve_path("notes/out.txt", cfg.root) == written.resolve()


async def test_read_resolves_relative_against_config_root(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("world", encoding="utf-8")
    tool = ReadFileTool(_ctx_with_root(tmp_path))
    out = content_of(await tool.run(ReadFileTool.Params(path="hello.txt")))
    assert out == "world"


def test_network_tools_ask_by_default() -> None:
    assert WebSearchTool.permission_default is Permission.ASK
    assert WebFetchTool.permission_default is Permission.ASK


# --- shell (real pwsh) -----------------------------------------------------


@pytest.mark.skipif(pwsh is None, reason="pwsh not installed")
async def test_run_shell_echo() -> None:
    result = await RunShellTool().run(RunShellTool.Params(command="Write-Output 'ping'"))
    assert not is_error(result)
    assert "ping" in content_of(result)
    assert "[exit 0]" in content_of(result)


@pytest.mark.skipif(pwsh is None, reason="pwsh not installed")
async def test_run_shell_nonzero_exit_is_error() -> None:
    result = await RunShellTool().run(RunShellTool.Params(command="exit 3"))
    assert is_error(result)
    assert "[exit 3]" in content_of(result)


@pytest.mark.skipif(pwsh is None, reason="pwsh not installed")
async def test_run_shell_timeout_is_error() -> None:
    result = await RunShellTool().run(
        RunShellTool.Params(command="Start-Sleep -Seconds 10", timeout_seconds=0.3)
    )
    assert is_error(result)
    assert "timed out" in content_of(result)


# --- web (mocked) ----------------------------------------------------------


def _ctx_with_tavily(key: str) -> ToolContext:
    secrets = Secrets(_env_file=None, tavily_api_key=key)  # type: ignore[call-arg]
    cfg = Config(
        root=Path.cwd(),
        models=ModelsConfig(),
        limits=LimitsConfig(),
        paths=PathsConfig(),
        secrets=secrets,
    )
    return ToolContext(config=cfg)


async def test_web_search_formats_results(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_search(api_key: str, query: str, max_results: int) -> dict:
        assert api_key == "tvly-key"
        return {
            "answer": "42.",
            "results": [
                {"title": "Result One", "url": "https://a.example", "content": "First snippet."},
                {"title": "Result Two", "url": "https://b.example", "content": "Second snippet."},
            ],
        }

    monkeypatch.setattr(web, "_tavily_search", fake_search)
    tool = WebSearchTool(_ctx_with_tavily("tvly-key"))
    out = content_of(await tool.run(WebSearchTool.Params(query="meaning of life")))
    assert "Answer: 42." in out
    assert "Result One" in out and "https://a.example" in out


async def test_web_search_without_key_is_error() -> None:
    tool = WebSearchTool(ToolContext(config=None))
    result = await tool.run(WebSearchTool.Params(query="x"))
    assert is_error(result)
    assert "TAVILY_API_KEY" in content_of(result)


async def test_web_fetch_extracts_main_text(monkeypatch: pytest.MonkeyPatch) -> None:
    html = """
    <html><head><title>Doc</title></head><body>
    <nav>menu junk</nav>
    <article>
      <h1>The Headline</h1>
      <p>This is the first substantial paragraph of the article, with enough words that
      the extractor treats it as the main content rather than boilerplate navigation.</p>
      <p>A second paragraph continues with more relevant detail and context for readers.</p>
    </article>
    <footer>copyright junk</footer>
    </body></html>
    """

    async def fake_fetch(url: str, timeout_seconds: float) -> str:
        return html

    monkeypatch.setattr(web, "_fetch_html", fake_fetch)
    out = content_of(await WebFetchTool().run(WebFetchTool.Params(url="https://x.example")))
    assert "https://x.example" in out
    assert "substantial paragraph" in out
    assert "menu junk" not in out  # boilerplate stripped


async def test_web_fetch_unextractable_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(url: str, timeout_seconds: float) -> str:
        return "<html><body></body></html>"

    monkeypatch.setattr(web, "_fetch_html", fake_fetch)
    result = await WebFetchTool().run(WebFetchTool.Params(url="https://empty.example"))
    assert is_error(result)


# --- untrusted-content framing (Phase 5 Task 9 hardening) ------------------


async def test_web_fetch_wraps_result_in_untrusted_framing(monkeypatch: pytest.MonkeyPatch) -> None:
    html = (
        "<html><body><article><h1>Guide</h1>"
        "<p>Here is a substantial paragraph of real article content that trafilatura will "
        "happily extract as the main body of the page for testing purposes.</p>"
        "</article></body></html>"
    )

    async def fake_fetch(url: str, timeout_seconds: float) -> str:
        return html

    monkeypatch.setattr(web, "_fetch_html", fake_fetch)
    out = content_of(await WebFetchTool().run(WebFetchTool.Params(url="https://x.example")))
    assert "NOT instructions" in out  # explicit framing present
    assert "--- begin fetched content (https://x.example, untrusted) ---" in out
    assert "--- end fetched content ---" in out
    assert "substantial paragraph" in out  # the actual content still delivered


async def test_web_search_wraps_results_in_untrusted_framing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(api_key: str, query: str, max_results: int) -> dict:
        return {"answer": "yes", "results": [{"title": "T", "url": "https://a.b", "content": "c"}]}

    monkeypatch.setattr(web, "_tavily_search", fake_search)
    tool = WebSearchTool(_ctx_with_tavily("tvly-key"))
    out = content_of(await tool.run(WebSearchTool.Params(query="q")))
    assert "NOT instructions" in out
    assert "--- begin search results (untrusted) ---" in out and "--- end search results ---" in out


async def test_read_file_stays_unwrapped(tmp_path: Path) -> None:
    # Documented tradeoff: workspace files are the user's own, so read_file is NOT
    # wrapped in untrusted framing (it would pollute code-reading flows); the
    # sensitive-path floor guards the dangerous targets instead.
    f = tmp_path / "notes.md"
    f.write_text("plain project notes", encoding="utf-8")
    out = content_of(await ReadFileTool().run(ReadFileTool.Params(path=str(f))))
    assert "untrusted" not in out and "NOT instructions" not in out
    assert "plain project notes" in out
