"""Artifacts Library screen pins (Phase 11 T11).

The global #artifacts screen: a filterable list + a preview panel. It reads (list + the hardened
/content route) and writes ONLY the existing pin/label metadata routes — no new authority. Content
(untrusted, cross-project) is rendered via textContent / a same-origin <img>; an external_uri is
shown as text, never opened or linkified.
"""

from __future__ import annotations

import re

from kira.ui.server import STATIC_DIR

ART = (STATIC_DIR / "screens" / "artifacts.js").read_text(encoding="utf-8")
APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_artifacts_registered_in_router() -> None:
    assert 'from "./screens/artifacts.js"' in APP
    assert "artifacts: renderArtifacts" in APP


def test_writes_are_pin_and_label_only() -> None:
    assert "/api/turn" not in ART and "/api/orchestration" not in ART
    posts = re.findall(r"api\.post\(`([^`]+)`", ART)
    assert posts, "expected pin/label POSTs"
    for p in posts:
        assert p.startswith("/api/artifacts/"), p
        assert p.endswith("/pin") or p.endswith("/label"), p


def test_renders_untrusted_content_without_innerhtml() -> None:
    # No innerHTML anywhere; text preview is a <pre> textContent, images via <img>.src.
    assert "innerHTML" not in ART
    assert 'el("pre"' in ART  # text/markdown preview is preformatted textContent
    assert "img.src = url" in ART  # image preview uses ONLY the same-origin content URL


def test_content_only_from_hardened_route_and_no_external_uri_open() -> None:
    assert "/api/artifacts/" in ART and "/content" in ART
    assert "window.open(" in ART and "noopener" in ART
    assert "window.open(a.external_uri" not in ART  # external_uri is metadata text, never opened
    # the preview fetch + window.open both target the encodeURIComponent'd content route
    assert "encodeURIComponent(a.id)" in ART


def test_metadata_block_surfaces_provenance() -> None:
    for field in ("content_hash", "provenance", "sensitivity", "origin"):
        assert field in ART, field
