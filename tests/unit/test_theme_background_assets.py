"""Theme background assets are local, compressed, and mapped only by the appearance theme."""

from jarvis.ui.server import STATIC_DIR

CSS = (STATIC_DIR / "kairo.css").read_text(encoding="utf-8")
ASSETS = STATIC_DIR / "assets"

THEME_BACKGROUNDS = {
    "light": "kairo-v2-bg-light.jpg",
    "noir": "kairo-v2-bg-noir.jpg",
    "neon": "kairo-v2-bg-neon.jpg",
}


def test_each_theme_uses_a_local_jpg_background_under_the_readability_veil() -> None:
    assert "var(--theme-background)" in CSS
    assert "linear-gradient(125deg, var(--veil-a)" in CSS
    for theme, filename in THEME_BACKGROUNDS.items():
        assert f':root[data-theme="{theme}"]' in CSS or theme == "noir"
        assert f'/static/assets/{filename}' in CSS
        assert (ASSETS / filename).is_file()


def test_theme_backgrounds_are_reasonably_sized_and_no_remote_assets_are_introduced() -> None:
    for filename in THEME_BACKGROUNDS.values():
        assert (ASSETS / filename).stat().st_size < 250_000
    assert "url(http" not in CSS and "url(//" not in CSS and "@import" not in CSS


def test_mobile_uses_scroll_attached_background_for_rendering_cost_and_crop_stability() -> None:
    assert "background-attachment: scroll" in CSS
    assert "center top" in CSS
