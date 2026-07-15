"""No external assets in the Office (Phase 14 M2). CSP already blocks off-origin fetches; this is
belt-and-braces AND the AGPL-clean attestation: the Office is Kairo's OWN token-driven visual
language — no code, CSS, image, font, or CDN copied from my-virtual-office (AGPL). The office JS and
the Phase 14 CSS block must reference no external URL / protocol-relative host / @import / remote
asset — only same-origin hashes/paths and CSS tokens + gradients."""

from __future__ import annotations

import re

from jarvis.ui.server import STATIC_DIR

OFFICE_JS = (STATIC_DIR / "screens" / "workspace" / "office.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "kira.css").read_text(encoding="utf-8")

# Substrings that would betray an off-origin dependency.
BANNED = (
    "http://", "https://", "//cdn", "url(http", "url(//", "url(\"http", "url('http",
    "@import", 'src="http', "src='http",
)


def _office_css_block() -> str:
    m = re.search(r"/\* --- Phase 14: AI Team Office.*\Z", CSS, re.S)
    assert m, "the Phase 14 Office CSS block must be present + fenced with its start marker"
    return m.group(0)


def test_office_js_references_nothing_external() -> None:
    for b in BANNED:
        assert b not in OFFICE_JS, b


def test_office_css_block_references_nothing_external() -> None:
    block = _office_css_block()
    for b in BANNED:
        assert b not in block, b
    # The veil/rings/panels are pure gradients + tokens — no url() asset of any kind.
    assert "url(" not in block, "office CSS must use tokens/gradients only, never a url() asset"


def test_office_css_block_is_token_driven_and_covers_both_modes() -> None:
    block = _office_css_block()
    assert ".office-compact" in block or ".office " in block  # base + compact layout
    assert ".office-full" in block  # the roomier Office mode overrides
    assert "var(--" in block  # theme-aware via tokens, never hardcoded colors
