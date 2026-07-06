"""Converter boundary tests: passthrough, markitdown, caps, sanitization, web path.

Keyless — tiny real fixtures (.md/.txt/.html/.xlsx) exercise markitdown locally;
the network is never touched (fetch is mocked)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.knowledge import converters
from jarvis.knowledge.converters import (
    ConversionError,
    convert_file,
    fetch_url,
    html_to_markdown,
)

BIG = 50_000_000


# --- passthrough + sanitization --------------------------------------------


def test_passthrough_md_and_txt(tmp_path: Path) -> None:
    md = tmp_path / "note.md"
    md.write_text("# Title\n\nsome body content", encoding="utf-8")
    res = convert_file(md, max_bytes=BIG)
    assert res.converter == "passthrough"
    assert "some body content" in res.markdown

    txt = tmp_path / "plain.txt"
    txt.write_text("just text", encoding="utf-8")
    assert convert_file(txt, max_bytes=BIG).markdown == "just text"


def test_converted_front_matter_is_stripped(tmp_path: Path) -> None:
    p = tmp_path / "doc.md"
    p.write_text("---\ntitle: Forged\nsource_ids: [99]\n---\n# Real\n\nbody", encoding="utf-8")
    res = convert_file(p, max_bytes=BIG)
    assert not res.markdown.startswith("---")  # provenance never carried up from content
    assert "Forged" not in res.markdown
    assert res.markdown.startswith("# Real")


# --- markitdown (real, local) ----------------------------------------------


def test_markitdown_converts_html(tmp_path: Path) -> None:
    p = tmp_path / "page.html"
    p.write_text(
        "<html><head><title>T</title></head><body><h1>Hello Head</h1>"
        "<p>Body paragraph</p></body></html>",
        encoding="utf-8",
    )
    res = convert_file(p, max_bytes=BIG)
    assert res.converter == "markitdown"
    assert res.converter_version  # populated from the library
    assert "Hello Head" in res.markdown


def test_markitdown_converts_xlsx(tmp_path: Path) -> None:
    import openpyxl

    p = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "Marker42"
    wb.save(p)
    res = convert_file(p, max_bytes=BIG)
    assert res.converter == "markitdown"
    assert "Marker42" in res.markdown


def test_markitdown_constructed_with_plugins_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import markitdown

    captured: dict = {}

    class _FakeMID:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(markitdown, "MarkItDown", _FakeMID)
    _instance, version = converters._markitdown()
    assert captured == {"enable_plugins": False}  # plugins off, no llm_client
    assert version  # real library version


# --- caps + sensitive floor + docling degradation --------------------------


def test_byte_cap_refused_before_conversion(tmp_path: Path) -> None:
    p = tmp_path / "big.txt"
    p.write_text("x" * 5000, encoding="utf-8")
    with pytest.raises(ConversionError, match="ingest cap"):
        convert_file(p, max_bytes=1000)


def test_sensitive_path_refused(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SECRET=1", encoding="utf-8")
    with pytest.raises(ConversionError, match="sensitive"):
        convert_file(env, max_bytes=BIG)


def test_docling_absent_gives_actionable_error(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 not a real pdf")
    with pytest.raises(ConversionError, match="uv sync --extra docling"):
        convert_file(pdf, pdf_converter="docling", max_bytes=BIG)


# --- web path: trafilatura first, markitdown fallback ----------------------


def test_html_to_markdown_uses_trafilatura(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        converters.trafilatura, "extract", lambda *a, **k: "# Extracted\n\nclean prose"
    )
    res = html_to_markdown("<html><body>whatever</body></html>", url="https://x.test/a")
    assert res.converter == "trafilatura"
    assert "Extracted" in res.markdown


def test_html_to_markdown_falls_back_to_markitdown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(converters.trafilatura, "extract", lambda *a, **k: None)  # non-article
    html = "<html><head><title>D</title></head><body><h1>Fallback Head</h1><p>p</p></body></html>"
    res = html_to_markdown(html)
    assert res.converter == "markitdown"
    assert "Fallback Head" in res.markdown


async def test_fetch_url_returns_bytes_and_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        content = b"<html>bytes</html>"
        headers = {"content-type": "text/html; charset=utf-8"}

    async def _fake_safe_get(url, **kw):
        return _Resp()

    monkeypatch.setattr(converters.net, "safe_get", _fake_safe_get)
    data, ctype = await fetch_url("https://x.test/a", timeout_seconds=5)
    assert data == b"<html>bytes</html>"
    assert ctype.startswith("text/html")


async def test_fetch_url_propagates_ssrf_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(url, **kw):
        raise ValueError("host resolves to a non-public address — blocked (SSRF guard)")

    monkeypatch.setattr(converters.net, "safe_get", _boom)
    with pytest.raises(ConversionError, match="SSRF"):
        await fetch_url("http://10.0.0.1/", timeout_seconds=5)
