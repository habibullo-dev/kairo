"""Deterministic, heading-aware Markdown chunking.

A pure function — no I/O, no model — so it is fully table-testable and produces
byte-identical output for identical input (the retrieval index must be stable
across rebuilds). The unit of retrieval is a *section*: content under one heading,
carrying its heading path as context.

Rules:

* Headings are ATX (``#``–``######``) and **fence-aware** — a ``#`` inside a
  ```` ``` ```` / ``~~~`` code fence is literal, never a heading.
* Each section becomes one chunk if it fits ``max_chars``; otherwise it is split
  greedily at blank-line paragraph boundaries, and a single paragraph longer than
  ``max_chars`` is hard-split.
* Tiny sections (< ``min_chars``) merge into an adjacent sibling (same parent
  heading) so heading-only fragments don't waste an embedding.

The stored chunk ``text`` is the clean section body; ``heading_path`` (e.g.
``"Rust async > Tokio"``) is metadata. Callers prefix the heading path onto the
text *at embed time* so the retrieval vector carries its context while the stored
text stays clean (see :func:`embed_text`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")


@dataclass(frozen=True)
class Chunk:
    """A retrieval unit: a section body plus its heading-path context and order."""

    heading_path: str
    seq: int
    text: str


def embed_text(chunk: Chunk) -> str:
    """The text to embed for a chunk: heading path prefixed onto the body, so the
    vector carries the section's context. The stored ``text`` stays clean."""
    return f"{chunk.heading_path}\n\n{chunk.text}" if chunk.heading_path else chunk.text


def _parent(heading_path: str) -> str:
    """The heading path with its last component dropped ('A > B' -> 'A', 'A' -> '')."""
    return heading_path.rsplit(" > ", 1)[0] if " > " in heading_path else ""


def _same_parent(a: str, b: str) -> bool:
    return _parent(a) == _parent(b)


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split Markdown into (heading_path, body) sections, fence-aware."""
    stack: list[tuple[int, str]] = []  # (level, title) of open ancestors
    sections: list[tuple[str, list[str]]] = []
    heading_path = ""
    body: list[str] = []
    in_fence = False

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            body.append(line)
            continue
        match = None if in_fence else _HEADING.match(line)
        if match:
            title = match.group(2).strip().rstrip("#").strip()
            if not title:  # an empty heading ('###   ') is just text
                body.append(line)
                continue
            sections.append((heading_path, body))
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            heading_path = " > ".join(t for _, t in stack)
            body = []
        else:
            body.append(line)
    sections.append((heading_path, body))
    return [(hp, "\n".join(lines)) for hp, lines in sections]


def _pack_paragraphs(body: str, max_chars: int) -> list[str]:
    """Greedily pack blank-line-delimited paragraphs into ≤ max_chars pieces; a
    single paragraph longer than max_chars is hard-split. Empty body -> no pieces."""
    paras = [p.strip() for p in _BLANK_LINE.split(body) if p.strip()]
    pieces: list[str] = []
    cur = ""
    for para in paras:
        if len(para) > max_chars:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.extend(para[k : k + max_chars] for k in range(0, len(para), max_chars))
        elif not cur:
            cur = para
        elif len(cur) + 2 + len(para) <= max_chars:
            cur = f"{cur}\n\n{para}"
        else:
            pieces.append(cur)
            cur = para
    if cur:
        pieces.append(cur)
    return pieces


def _merge_small(raw: list[tuple[str, str]], min_chars: int) -> list[tuple[str, str]]:
    """Fold each undersized chunk forward into the next same-parent sibling (taking
    that sibling's heading path); an undersized final chunk merges backward. A tiny
    chunk with no mergeable neighbor is kept rather than dropping its content."""
    result: list[tuple[str, str]] = []
    carry: tuple[str, str] | None = None
    for hp, text in raw:
        if carry is not None:
            if _same_parent(carry[0], hp):
                text = f"{carry[1]}\n\n{text}"  # fold carry into this sibling
            else:
                result.append(carry)  # different topic — can't merge; keep the orphan
            carry = None
        if len(text) < min_chars:
            carry = (hp, text)
        else:
            result.append((hp, text))
    if carry is not None:
        if result and _same_parent(result[-1][0], carry[0]):
            prev_hp, prev_text = result[-1]
            result[-1] = (prev_hp, f"{prev_text}\n\n{carry[1]}")
        else:
            result.append(carry)
    return result


def chunk_markdown(text: str, *, max_chars: int = 2000, min_chars: int = 200) -> list[Chunk]:
    """Split Markdown into heading-aware chunks. Pure and deterministic.

    Empty or heading-only input yields no chunks (nothing to retrieve). ``seq`` is
    a monotonic 0-based index over the returned chunks."""
    raw: list[tuple[str, str]] = []
    for heading_path, body in _split_sections(text):
        for piece in _pack_paragraphs(body, max_chars):
            raw.append((heading_path, piece))
    merged = _merge_small(raw, min_chars)
    return [Chunk(heading_path=hp, seq=i, text=t) for i, (hp, t) in enumerate(merged)]
