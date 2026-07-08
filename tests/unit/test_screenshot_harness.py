"""Keyless tests for the screenshot DoD machinery (Phase 11 T2): the pure browser-free helpers
in jarvis.ui.screenshots, and the Playwright driver's graceful degradation via the injectable
seam (no browser install required)."""

from __future__ import annotations

import pytest

from jarvis.services.playwright_driver import (
    PlaywrightInspectDriver,
    _safe_shot_name,
    _summarize_a11y,
    install_if_available,
    playwright_available,
)
from jarvis.services.playwright_local import _NotInstalledDriver, set_driver
from jarvis.ui.screenshots import (
    THEMES,
    VIEWPORTS,
    analyze_overlap,
    screenshot_name,
)


# --- viewport matrix + themes ---------------------------------------------------------
def test_viewport_widths_are_desktop_laptop_mobile() -> None:
    assert tuple(w for w, _h in VIEWPORTS) == (1440, 1024, 390)


def test_themes_are_the_three_token_sets() -> None:
    assert THEMES == ("light", "noir", "neon")


# --- filename convention --------------------------------------------------------------
def test_screenshot_name_convention() -> None:
    assert screenshot_name("daily", "empty", "noir", 1440) == "daily__empty__noir-1440w.png"


def test_screenshot_name_slugs_out_path_separators() -> None:
    name = screenshot_name("a/b\\c", "needs review", "Noir", 390)
    assert "/" not in name and "\\" not in name and " " not in name
    assert name.endswith("-390w.png")


# --- overlap / horizontal-scroll analysis (pure) --------------------------------------
def test_analyze_overlap_clean_page() -> None:
    assert analyze_overlap({"innerWidth": 1440, "scrollWidth": 1440, "offenders": []}) == []


def test_analyze_overlap_within_slack() -> None:
    # 2px slack absorbs sub-pixel rounding / scrollbar gutters.
    assert analyze_overlap({"innerWidth": 390, "scrollWidth": 392, "offenders": []}) == []


def test_analyze_overlap_flags_horizontal_overflow() -> None:
    v = analyze_overlap({"innerWidth": 390, "scrollWidth": 520, "offenders": []})
    assert len(v) == 1 and "horizontal overflow" in v[0]


def test_analyze_overlap_flags_offending_elements() -> None:
    v = analyze_overlap({
        "innerWidth": 1024,
        "scrollWidth": 1024,
        "offenders": [{"tag": "div", "cls": "wide-card", "right": 1300}],
    })
    assert len(v) == 1 and "wide-card" in v[0] and "1300" in v[0]


# --- driver helpers -------------------------------------------------------------------
def test_safe_shot_name_is_a_clean_png() -> None:
    name = _safe_shot_name("http://127.0.0.1:8787/#workspace/3?x=1")
    assert name.endswith(".png") and "/" not in name and ":" not in name and "?" not in name


def test_summarize_a11y_handles_empty_and_tree() -> None:
    assert "no accessibility tree" in _summarize_a11y(None)
    tree = {"role": "WebArea", "name": "Kairo", "children": [{"role": "button", "children": []}]}
    assert "2 nodes" in _summarize_a11y(tree) and "WebArea" in _summarize_a11y(tree)


# --- graceful degradation via the injectable seam -------------------------------------
def test_driver_has_async_inspect() -> None:
    # Structural check (InspectDriver is a non-runtime-checkable Protocol): the real driver
    # exposes an async inspect() even without playwright present.
    import inspect as _pyinspect

    assert _pyinspect.iscoroutinefunction(PlaywrightInspectDriver().inspect)


def test_install_if_available_matches_availability_and_restores() -> None:
    try:
        assert install_if_available(screenshot_dir=None) == playwright_available()
    finally:
        set_driver(_NotInstalledDriver())  # never leave a real driver wired for other tests


async def test_driver_lazy_import_degrades_without_playwright() -> None:
    if playwright_available():
        pytest.skip("playwright installed — the lazy-import degradation path is not exercised")
    # Without the extra, invoking the driver raises at the lazy import (never a silent no-op),
    # and PlaywrightInspectTool catches that and returns a clean tool error.
    with pytest.raises(ModuleNotFoundError):
        await PlaywrightInspectDriver().inspect("navigate", "http://127.0.0.1:8787/", "")
