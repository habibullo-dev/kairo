"""Pure, browser-free helpers for the screenshot definition-of-done (Phase 11 R4).

The Playwright-driving harness (``tests/ui/capture.py``) is a standalone dev tool that needs the
``browser`` extra; everything *testable without a browser* lives here so it is covered by keyless
unit tests: the viewport matrix, the theme set, the shot-filename convention, and the no-overlap
/ no-horizontal-scroll analysis (a pure function over metrics the harness measures in-page).
"""

from __future__ import annotations

import re

#: Viewport widths the DoD asserts every primary screen against (desktop / laptop / mobile).
#: (width, height) — the width is what the no-overlap check and the filename use.
VIEWPORTS: tuple[tuple[int, int], ...] = ((1440, 900), (1024, 768), (390, 844))

#: The three themes the token system ships (Phase 11 T5). Screenshots are taken per theme.
THEMES: tuple[str, ...] = ("light", "noir", "neon")

#: Pixel slack: sub-pixel rounding and scrollbar gutters shouldn't count as overflow.
_OVERFLOW_SLACK_PX = 2

_SAFE = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    return _SAFE.sub("-", value.lower()).strip("-") or "x"


def screenshot_name(screen: str, state: str, theme: str, width: int) -> str:
    """The DoD filename: ``{screen}__{state}__{theme}-{width}w.png``. Components are slugified so
    a screen/state label can never inject a path separator."""
    return f"{_slug(screen)}__{_slug(state)}__{_slug(theme)}-{int(width)}w.png"


#: Evaluated in-page (page.evaluate) to measure horizontal overflow + elements that extend past
#: the viewport. Returns a JSON-able dict consumed by :func:`analyze_overlap`.
OVERLAP_PROBE_JS = """
() => {
  const iw = window.innerWidth;
  const sw = document.documentElement.scrollWidth;
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.right > iw + 1) {
      offenders.push({
        tag: el.tagName.toLowerCase(),
        cls: (typeof el.className === 'string' ? el.className : '') || '',
        right: Math.round(r.right),
      });
      if (offenders.length >= 20) break;
    }
  }
  return { innerWidth: iw, scrollWidth: sw, offenders };
}
"""


def analyze_overlap(metrics: dict) -> list[str]:
    """Turn measured page metrics into a list of layout violations (empty == clean).

    Two checks: (1) the document must not scroll horizontally (``scrollWidth <= innerWidth``);
    (2) no visible element may extend past the right edge of the viewport. ``_OVERFLOW_SLACK_PX``
    absorbs sub-pixel rounding / scrollbar gutters.
    """
    violations: list[str] = []
    iw = int(metrics.get("innerWidth", 0))
    sw = int(metrics.get("scrollWidth", 0))
    if sw > iw + _OVERFLOW_SLACK_PX:
        violations.append(f"horizontal overflow: scrollWidth {sw}px > innerWidth {iw}px")
    for off in metrics.get("offenders", []):
        cls = off.get("cls", "")
        label = f"{off.get('tag', '?')}" + (f".{cls}" if cls else "")
        violations.append(f"element <{label}> extends to {off.get('right')}px (> {iw}px viewport)")
    return violations
