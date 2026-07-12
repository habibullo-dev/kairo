"""Deterministic, local source-file relationship extraction for project uploads.

This is deliberately a *structural* index, not an LLM interpretation of a repository:

* only import specifiers from a bounded prefix of supported source files are inspected;
* only specifiers that resolve to another uploaded file in the **same** project become edges;
* uploaded text is never imported, evaluated, or executed; and
* unresolved packages, aliases, generated code, and dynamic imports remain absent rather than
  being guessed into a misleading graph.

The output is used both by the rebuildable graph cache and to give the existing knowledge-query
tool a small amount of dependency context around semantically retrieved project files.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

_HEAD_CHARS = 24_000
_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
_JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte"})
_JS_RESOLUTION_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".json")

_PY_IMPORT = re.compile(r"^\s*import\s+(.+?)\s*$", re.MULTILINE)
_PY_FROM_IMPORT = re.compile(
    r"^\s*from\s+(?P<module>\.*[A-Za-z_][\w.]*)\s+import\s+(?P<names>[^#\n]+)",
    re.MULTILINE,
)
_JS_IMPORT = re.compile(
    r"(?:^|[;\n])\s*(?:import\s+(?:[\s\S]*?\s+from\s+)?|export\s+(?:[\s\S]*?\s+from\s+)?|"
    r"(?:const|let|var)\s+\w+\s*=\s*require\s*\(|import\s*\()\s*[\"']([^\"']+)[\"']",
    re.MULTILINE,
)


@dataclass(frozen=True)
class SourceHead:
    """The minimal, non-persistent input needed to derive file relationships."""

    source_id: int
    title: str | None
    text: str | None


def local_import_pairs(sources: Iterable[SourceHead]) -> list[tuple[int, int]]:
    """Return sorted ``(importer_source_id, imported_source_id)`` pairs.

    Titles are logical browser-upload paths, never server paths.  Invalid or duplicate paths are
    ignored rather than being normalized into a surprising relationship.  Each source contributes
    only its first :data:`_HEAD_CHARS`, where conventional static imports belong.
    """
    rows = [source for source in sources if _logical_path(source.title) is not None]
    by_path: dict[str, int] = {}
    for source in rows:
        path = _logical_path(source.title)
        assert path is not None
        # A live source origin is normally unique.  Keep the first id if a legacy/corrupt set is
        # not: choosing arbitrarily would make an import edge nondeterministic.
        if path not in by_path:
            by_path[path] = source.source_id
    modules: dict[str, int] = {}
    for path, source_id in by_path.items():
        for name in _python_module_names(path):
            modules.setdefault(name, source_id)

    pairs: set[tuple[int, int]] = set()
    for source in rows:
        path = _logical_path(source.title)
        assert path is not None
        text = (source.text or "")[:_HEAD_CHARS]
        suffix = PurePosixPath(path).suffix.lower()
        targets: set[int] = set()
        if suffix in _PYTHON_SUFFIXES:
            for specifier in _python_specifiers(path, text):
                target = modules.get(specifier)
                if target is not None:
                    targets.add(target)
        elif suffix in _JS_SUFFIXES:
            for specifier in _JS_IMPORT.findall(text):
                target = _resolve_js_specifier(path, specifier, by_path)
                if target is not None:
                    targets.add(target)
        for target in targets:
            if target != source.source_id:
                pairs.add((source.source_id, target))
    return sorted(pairs)


def _logical_path(value: str | None) -> str | None:
    raw = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _python_module_names(path: str) -> tuple[str, ...]:
    pure = PurePosixPath(path)
    if pure.suffix.lower() not in _PYTHON_SUFFIXES:
        return ()
    parts = list(pure.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    if not parts:
        return ()
    names = [".".join(parts)]
    # Conventional source roots make `from package import module` resolve without baking a
    # repository name into the import.  The last src segment is intentional for nested examples.
    if "src" in parts:
        root = len(parts) - 1 - parts[::-1].index("src")
        if root + 1 < len(parts):
            names.append(".".join(parts[root + 1 :]))
    return tuple(dict.fromkeys(name for name in names if name))


def _python_specifiers(path: str, text: str) -> set[str]:
    current = _python_module_names(path)
    current_module = current[-1] if current else ""
    package = current_module.rsplit(".", 1)[0] if "." in current_module else ""
    out: set[str] = set()
    for match in _PY_IMPORT.finditer(text):
        for item in match.group(1).split(","):
            name = item.strip().split(" as ", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name):
                out.add(name)
    for match in _PY_FROM_IMPORT.finditer(text):
        raw_module = match.group("module")
        module = _resolve_python_relative(package, raw_module)
        if not module:
            continue
        out.add(module)
        # `from package import submodule` is ambiguous until resolved; include it only as a
        # candidate, never as an assertion.  A non-module imported symbol simply has no edge.
        for item in match.group("names").split(","):
            name = item.strip().split(" as ", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_]\w*", name) and name != "*":
                out.add(f"{module}.{name}")
    return out


def _resolve_python_relative(package: str, raw_module: str) -> str | None:
    dots = len(raw_module) - len(raw_module.lstrip("."))
    tail = raw_module[dots:]
    if not dots:
        return tail
    components = [part for part in package.split(".") if part]
    # One dot means the current package; two means its parent, and so on.
    up = dots - 1
    if up > len(components):
        return None
    base = components[: len(components) - up]
    return ".".join((*base, tail)) if tail else ".".join(base)


def _resolve_js_specifier(path: str, specifier: str, by_path: dict[str, int]) -> int | None:
    # Package/alias imports are intentionally unresolved.  They could be external, configured by
    # an arbitrary build tool, or point to generated code; guessing would make the map dishonest.
    if not specifier.startswith("."):
        return None
    candidate = posixpath.normpath(posixpath.join(posixpath.dirname(path), specifier)).strip("/")
    if candidate.startswith("../") or candidate in {"", "."}:
        return None
    choices = [candidate]
    if not PurePosixPath(candidate).suffix:
        choices.extend(candidate + suffix for suffix in _JS_RESOLUTION_SUFFIXES)
        choices.extend(candidate + "/index" + suffix for suffix in _JS_RESOLUTION_SUFFIXES)
    for choice in choices:
        target = by_path.get(choice)
        if target is not None:
            return target
    return None
