"""Chunking tests: heading-aware, fence-aware, deterministic. Pure — no fixtures."""

from __future__ import annotations

from jarvis.knowledge.chunking import Chunk, chunk_markdown, embed_text


def test_heading_path_stack() -> None:
    md = "# Top\n\nintro body here\n\n## Sub\n\nsub body here\n\n### Deep\n\ndeep body here\n"
    chunks = chunk_markdown(md, max_chars=100, min_chars=1)
    paths = [c.heading_path for c in chunks]
    assert paths == ["Top", "Top > Sub", "Top > Sub > Deep"]


def test_sibling_heading_pops_stack() -> None:
    md = "# A\n\nbody of a section\n\n## A1\n\nbody a1\n\n# B\n\nbody of b section\n"
    chunks = chunk_markdown(md, max_chars=100, min_chars=1)
    assert [c.heading_path for c in chunks] == ["A", "A > A1", "B"]


def test_hash_inside_code_fence_is_not_a_heading() -> None:
    md = (
        "# Real\n\nsome intro text that is long enough to stand alone as a chunk here\n\n"
        "```python\n# this is a comment, not a heading\nx = 1\n```\n\nmore body text afterwards\n"
    )
    chunks = chunk_markdown(md, max_chars=500, min_chars=1)
    # only one heading -> everything lives under 'Real'; the fenced '#' didn't split
    assert all(c.heading_path == "Real" for c in chunks)
    assert any("not a heading" in c.text for c in chunks)


def test_paragraph_greedy_split() -> None:
    # three ~40-char paragraphs, max 100 -> packs 2 then 1 (greedy)
    p = "x" * 40
    md = f"# H\n\n{p}\n\n{p}\n\n{p}\n"
    chunks = chunk_markdown(md, max_chars=100, min_chars=1)
    assert len(chunks) == 2
    assert all(len(c.text) <= 100 for c in chunks)
    assert [c.seq for c in chunks] == [0, 1]


def test_giant_paragraph_hard_split() -> None:
    md = "# H\n\n" + "y" * 250 + "\n"
    chunks = chunk_markdown(md, max_chars=100, min_chars=1)
    assert len(chunks) == 3  # 250 / 100 -> 100, 100, 50
    assert [len(c.text) for c in chunks] == [100, 100, 50]


def test_small_sections_merge_forward() -> None:
    # two tiny sibling sections merge into one chunk (< min_chars each)
    md = "# Top\n\n## A\n\ntiny\n\n## B\n\nalso small but combined exceeds the floor here\n"
    chunks = chunk_markdown(md, max_chars=500, min_chars=30)
    assert len(chunks) == 1
    assert "tiny" in chunks[0].text and "also small" in chunks[0].text


def test_tiny_final_section_merges_backward() -> None:
    # a tiny FINAL sibling (no next sibling to fold into) merges backward into the
    # previous sibling that shares its parent ('Top').
    md = "# Top\n\n## A\n\n" + "z" * 60 + "\n\n## B\n\ntiny\n"
    chunks = chunk_markdown(md, max_chars=500, min_chars=30)
    assert len(chunks) == 1
    assert chunks[0].heading_path == "Top > A"
    assert "tiny" in chunks[0].text


def test_different_parent_tiny_not_merged() -> None:
    # a tiny chunk under a different top-level topic is kept, not merged across topics
    md = "# A\n\n## A1\n\ntiny\n\n# B\n\n" + "b" * 60 + "\n"
    chunks = chunk_markdown(md, max_chars=500, min_chars=30)
    paths = [c.heading_path for c in chunks]
    assert "A > A1" in paths and "B" in paths  # the orphan survives on its own


def test_empty_and_heading_only_docs() -> None:
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []
    assert chunk_markdown("# Just A Heading\n\n## And Another\n") == []  # no bodies


def test_preamble_before_first_heading() -> None:
    md = "some preamble text long enough to be its own chunk before any heading\n\n# H\n\nbody\n"
    chunks = chunk_markdown(md, max_chars=500, min_chars=1)
    assert chunks[0].heading_path == ""
    assert "preamble" in chunks[0].text


def test_determinism() -> None:
    md = "# A\n\n" + "\n\n".join("para " + "w" * 50 for _ in range(6)) + "\n\n## B\n\ntail body\n"
    first = chunk_markdown(md, max_chars=120, min_chars=20)
    second = chunk_markdown(md, max_chars=120, min_chars=20)
    assert first == second  # frozen dataclasses compare by value


def test_seq_is_monotonic() -> None:
    md = "# A\n\n" + "\n\n".join("x" * 80 for _ in range(5)) + "\n"
    chunks = chunk_markdown(md, max_chars=100, min_chars=1)
    assert [c.seq for c in chunks] == list(range(len(chunks)))


def test_embed_text_prefixes_heading_path() -> None:
    assert embed_text(Chunk("A > B", 0, "body")) == "A > B\n\nbody"
    assert embed_text(Chunk("", 0, "preamble")) == "preamble"  # no path -> no prefix
