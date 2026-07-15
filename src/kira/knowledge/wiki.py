"""Obsidian-compatible wiki pages: the jail and the front-matter policy.

Pure helpers (no store, no embedder, no clock) so the two security-critical pieces
— the path jail and the front-matter merge — are unit-tested directly, before any
tool can call them.

* :func:`safe_wiki_path` confines a page to the wiki directory. It rejects absolute
  / drive / UNC inputs, ``..`` escapes, symlinks pointing out, Windows ADS streams
  (``page.md:stream``), reserved device names (``CON``, ``NUL``, …), trailing
  dot/space (silently stripped by Windows), and anything but a ``.md`` suffix.
* Front-matter is Kira's provenance record, so it is generated from database
  state — never carried up from page *content*. But a page on disk is an
  Obsidian-editable file: :func:`build_front_matter` regenerates only the keys
  Kira owns and **preserves every other key verbatim** (``tags``, ``aliases``,
  plugin keys), and never regenerates a stable ``id`` once assigned.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kira.paths import is_sensitive_path

# Keys Kira owns and regenerates on every write. Everything else in a page's
# front-matter (tags, aliases, cssclass, plugin keys, …) is preserved verbatim.
KIRA_KEYS: frozenset[str] = frozenset(
    {"id", "title", "source_ids", "created", "updated", "created_by"}
)

_WINDOWS_RESERVED: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_FRONT_MATTER = re.compile(r"\A﻿?---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)


class WikiPathError(ValueError):
    """A page path that escapes the jail or is unsafe. Message is model-readable."""


def safe_wiki_path(wiki_dir: Path, page: str) -> Path:
    """Resolve ``page`` (wiki-relative) to an absolute path *inside* ``wiki_dir``, or
    raise :class:`WikiPathError`. This is containment, not policy — see the module
    docstring for the full list of rejected shapes."""
    raw = page.strip()
    if not raw:
        raise WikiPathError("empty page path")
    if not raw.lower().endswith(".md"):
        raise WikiPathError(f"wiki pages must end in .md (got {page!r})")
    if raw.startswith(("/", "\\", "~")) or ":" in raw:
        raise WikiPathError(
            f"wiki page must be a relative path with no drive, UNC, or stream (got {page!r})"
        )
    for part in re.split(r"[\\/]+", raw):
        if part in ("", ".", ".."):
            continue  # '..' is caught by the containment check after resolve()
        if part.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise WikiPathError(f"'{part}' uses a reserved device name")
        if part != part.rstrip(". "):
            raise WikiPathError(f"'{part}' has a trailing dot or space (Windows strips these)")

    wiki_root = wiki_dir.resolve()
    target = (wiki_root / raw).resolve()  # collapses '..'; follows symlinks
    if target != wiki_root and not target.is_relative_to(wiki_root):
        raise WikiPathError(f"wiki page escapes the wiki directory (got {page!r})")
    if is_sensitive_path(target):
        raise WikiPathError(f"refusing a sensitive wiki path: {target}")
    return target


def split_front_matter(text: str) -> tuple[dict, str]:
    """Split a page into (front_matter dict, body). No front-matter -> ({}, text).

    Parsed with ``yaml.safe_load`` only (never ``yaml.load`` — page content is
    attacker-reachable). Malformed or non-mapping front-matter yields ``({}, text)``
    so a bad block is treated as body, never executed."""
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, match.group(2)


def render_page(front_matter: dict, body: str) -> str:
    """Render front-matter + body to a page. ``allow_unicode`` keeps titles readable;
    ``sort_keys=False`` preserves the managed-first, preserved-after order."""
    fm = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=True).strip()
    body = body.strip("\n")
    return f"---\n{fm}\n---\n\n{body}\n" if body else f"---\n{fm}\n---\n"


def slugify(text: str) -> str:
    """A stable id slug from a title/stem: lowercase, non-alphanumerics to hyphens."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "page"


def build_front_matter(
    existing: dict,
    *,
    title: str,
    source_ids: list[int],
    created_by: str,
    now: str,
    slug_seed: str,
) -> dict:
    """Merge Kira-managed front-matter over ``existing`` while preserving every
    unknown (Obsidian/plugin) key. ``id`` and ``created`` are kept if already set
    (stable across rewrites); ``updated`` is always stamped."""
    managed = {
        "id": existing.get("id") or slugify(slug_seed),
        "title": title,
        "source_ids": list(source_ids),
        "created": existing.get("created") or now,
        "updated": now,
        "created_by": existing.get("created_by") or created_by,
    }
    preserved = {k: v for k, v in existing.items() if k not in KIRA_KEYS}
    return {**managed, **preserved}  # managed first for readability; unknown keys kept
