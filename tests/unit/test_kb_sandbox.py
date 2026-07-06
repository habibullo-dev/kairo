"""Converter sandbox tests: killable subprocess + zip-bomb pre-scan.

The subprocess round-trip and timeout-kill tests run a real child process
(``python -m jarvis.knowledge.convert_worker``); the archive checks are pure."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from jarvis.knowledge import converters
from jarvis.knowledge.converters import (
    ConversionError,
    check_archive_safety,
    convert_file_sandboxed,
)

BIG = 50_000_000


# --- subprocess sandbox ----------------------------------------------------


async def test_sandbox_converts_html_in_subprocess(tmp_path: Path) -> None:
    p = tmp_path / "page.html"
    p.write_text("<html><body><h1>Sandbox Head</h1><p>body</p></body></html>", encoding="utf-8")
    result = await convert_file_sandboxed(p, max_bytes=BIG, timeout_seconds=60)
    assert result.converter == "markitdown"
    assert "Sandbox Head" in result.markdown


async def test_sandbox_reports_conversion_error_from_child(tmp_path: Path) -> None:
    # a sensitive path is refused inside the child and surfaces as a clean error
    # (the parent pre-check would also catch it; here we exercise the child path by
    # pointing at a .pdf routed to absent docling)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 nope")
    with pytest.raises(ConversionError, match="uv sync --extra docling"):
        await convert_file_sandboxed(
            pdf, max_bytes=BIG, pdf_converter="docling", timeout_seconds=60
        )


async def test_sandbox_kills_a_hung_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the env-gated self-test hook makes the worker sleep; the parent must kill it at
    # the deadline and report honestly (a thread-based timeout could not do this)
    monkeypatch.setenv("JARVIS_CONVERT_SELFTEST_SLEEP", "30")
    p = tmp_path / "page.html"
    p.write_text("<html><body><h1>x</h1></body></html>", encoding="utf-8")
    with pytest.raises(ConversionError, match="exceeded|terminated"):
        await convert_file_sandboxed(p, max_bytes=BIG, timeout_seconds=0.5)


async def test_sandbox_passthrough_skips_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # passthrough text needs no parser -> no child process; prove it by making any
    # spawn explode and converting a .md anyway
    async def _no_spawn(*_a, **_kw):
        raise AssertionError("passthrough must not spawn a subprocess")

    monkeypatch.setattr(converters.asyncio, "create_subprocess_exec", _no_spawn)
    p = tmp_path / "note.md"
    p.write_text("# Title\n\nbody", encoding="utf-8")
    result = await convert_file_sandboxed(p, max_bytes=BIG, timeout_seconds=60)
    assert result.converter == "passthrough"
    assert "body" in result.markdown


# --- archive (zip bomb) pre-scan -------------------------------------------


def _zip_with(tmp_path: Path, name: str, members: dict[str, bytes]) -> Path:
    p = tmp_path / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        for member_name, data in members.items():
            zf.writestr(member_name, data)
    return p


def test_archive_bomb_refused_on_uncompressed_size(tmp_path: Path) -> None:
    # 2 MB of zeros compresses to ~nothing; the pre-scan reads the declared
    # uncompressed size from the central directory (no extraction) and refuses
    p = _zip_with(tmp_path, "bomb.zip", {"big.txt": b"\0" * 2_000_000})
    with pytest.raises(ConversionError, match="decompression bomb"):
        check_archive_safety(p, max_uncompressed_bytes=1_000_000)
    # under a generous cap it passes
    check_archive_safety(p, max_uncompressed_bytes=10_000_000)


def test_nested_archive_refused(tmp_path: Path) -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("x.txt", b"hi")
    p = _zip_with(tmp_path, "outer.docx", {"nested.zip": inner.getvalue(), "ok.xml": b"<x/>"})
    with pytest.raises(ConversionError, match="nested archives"):
        check_archive_safety(p, max_uncompressed_bytes=10_000_000)


def test_non_zip_with_archive_suffix_is_ignored(tmp_path: Path) -> None:
    p = tmp_path / "corrupt.docx"
    p.write_bytes(b"not actually a zip")
    check_archive_safety(p, max_uncompressed_bytes=1000)  # no raise — left to the converter


def test_precheck_derived_cap_catches_bomb_via_sandbox(tmp_path: Path) -> None:
    # the derived cap (max_bytes*4, floor 100MB) is applied through convert_file's
    # pre-check for archive suffixes; a synthetic 200MB-uncompressed member trips it
    # even though the file on disk is tiny.
    p = _zip_with(tmp_path, "big.docx", {"payload.xml": b"\0" * 120_000_000})
    with pytest.raises(ConversionError, match="decompression bomb"):
        converters.convert_file(p, max_bytes=1_000_000)  # cap -> max(4MB, 100MB) = 100MB
