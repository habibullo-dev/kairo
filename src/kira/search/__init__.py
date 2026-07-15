"""Federated full-text search over the Phase 11 FTS domains (snippets only, scoped in SQL)."""

from __future__ import annotations

from kira.search.service import SearchResult, search

__all__ = ["SearchResult", "search"]
