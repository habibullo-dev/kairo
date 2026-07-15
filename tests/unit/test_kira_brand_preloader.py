"""Kira brand assets and startup preloader stay local, safe, and fail-open."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from jarvis.ui.server import STATIC_DIR

ASSETS = STATIC_DIR / "assets"
INDEX = STATIC_DIR / "index.html"
AUTH = STATIC_DIR / "auth.html"
WORKSTATION_CSS = STATIC_DIR / "kira.css"
AUTH_CSS = STATIC_DIR / "auth" / "auth.css"
PRELOADER_CSS = STATIC_DIR / "kira-preloader.css"
BOOT = STATIC_DIR / "ui" / "boot.js"
APP = STATIC_DIR / "app.js"

MARKS = {
    "on dark": ASSETS / "kira-mark-on-dark.svg",
    "on light": ASSETS / "kira-mark-on-light.svg",
    "favicon": ASSETS / "kira-favicon.svg",
}
BACKGROUNDS = {
    "light": (
        ASSETS / "kira-workspace-bg-light.jpg",
        "1d2ec121f58d40884f95ed0d00070cac5145b733a20bb7e836c49bd1dd62c7f5",
    ),
    "noir": (
        ASSETS / "kira-workspace-bg-noir.jpg",
        "3b62317a669afd5eac592771a464d639987306b4ba1645fc185fe1b6af195ddc",
    ),
    "neon": (
        ASSETS / "kira-workspace-bg-neon.jpg",
        "3807966ea30011338c876cd14a7a30478969038aaec0516ca9e11a695ac971df",
    ),
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _css_block(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(?P<body>[^}]*)\}", css)
    assert match is not None, f"missing CSS selector: {selector}"
    return match.group("body")


class _LoaderMarkup(HTMLParser):
    """Collect the loader subtree without adding an HTML parser dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_body = False
        self.first_body_tag: tuple[str, dict[str, str | None]] | None = None
        self.loader_depth = 0
        self.loader_attrs: dict[str, str | None] = {}
        self.classes: set[str] = set()
        self.focusable_tags: list[str] = []
        self.loader_roles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "body":
            self.in_body = True
            return
        if not self.in_body:
            return
        if self.first_body_tag is None:
            self.first_body_tag = (tag, values)
        classes = set((values.get("class") or "").split())
        if self.loader_depth:
            self.loader_depth += 1
        elif "kira-preloader" in classes:
            self.loader_depth = 1
            self.loader_attrs = values
        if not self.loader_depth:
            return
        self.classes.update(classes)
        if tag in {"a", "button", "input", "select", "textarea"}:
            self.focusable_tags.append(tag)
        if values.get("tabindex") not in {None, "-1"}:
            self.focusable_tags.append(f"{tag}[tabindex]")
        if values.get("role"):
            self.loader_roles.append(values["role"] or "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.loader_depth:
            self.loader_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "body":
            self.in_body = False
            return
        if self.loader_depth:
            self.loader_depth -= 1


def test_kira_brand_asset_set_is_complete_and_old_named_assets_are_gone() -> None:
    expected = [*MARKS.values(), *(item[0] for item in BACKGROUNDS.values()), PRELOADER_CSS, BOOT]
    for path in expected:
        assert path.is_file(), f"missing Kira asset: {path.relative_to(STATIC_DIR)}"
        assert path.stat().st_size > 0, f"empty Kira asset: {path.relative_to(STATIC_DIR)}"

    for old_name in (
        "kairo-mark-dark.svg",
        "kairo-mark-light.svg",
        "kairo-favicon.svg",
        "kairo-v2-bg-light.jpg",
        "kairo-v2-bg-noir.jpg",
        "kairo-v2-bg-neon.jpg",
    ):
        assert not (ASSETS / old_name).exists(), f"stale public brand asset remains: {old_name}"


def test_supplied_logo_geometry_is_cropped_branded_and_safe_for_direct_serving() -> None:
    geometry: set[str] = set()
    for surface, path in MARKS.items():
        raw = path.read_text(encoding="utf-8")
        assert path.stat().st_size < 30_000
        assert "kairo" not in raw.lower() and "jarvis" not in raw.lower()
        root = ET.fromstring(raw)
        assert _local_name(root.tag) == "svg"
        assert root.attrib.get("viewBox") == "206 193 500 500"

        all_text = " ".join((node.text or "") for node in root.iter()).lower()
        assert "kira" in all_text, f"{surface} mark needs a Kira title/description"
        paths = [node.attrib["d"] for node in root.iter() if _local_name(node.tag) == "path"]
        assert len(paths) == 1 and len(paths[0]) > 2_000
        geometry.add(re.sub(r"\s+", " ", paths[0]).strip())

        for node in root.iter():
            assert _local_name(node.tag) not in {"script", "foreignObject"}
            for qualified_name, value in node.attrib.items():
                name = _local_name(qualified_name).lower()
                assert not name.startswith("on"), f"event attribute {name} in {path.name}"
                lowered = value.lower()
                assert "javascript:" not in lowered and "data:" not in lowered
                assert "http://" not in lowered and "https://" not in lowered
                if name == "href":
                    assert value.startswith("#"), f"external SVG reference in {path.name}"
                assert "url(" not in lowered, f"external paint/filter reference in {path.name}"
    assert len(geometry) == 1, "all Kira surfaces must use the supplied monogram geometry"


def test_workspace_backgrounds_are_lossless_local_renames() -> None:
    for theme, (path, expected_sha256) in BACKGROUNDS.items():
        content = path.read_bytes()
        assert content.startswith(b"\xff\xd8") and content.endswith(b"\xff\xd9"), theme
        assert len(content) < 250_000, theme
        assert hashlib.sha256(content).hexdigest() == expected_sha256, theme


def test_three_themes_and_auth_shell_map_to_the_correct_local_marks() -> None:
    css = WORKSTATION_CSS.read_text(encoding="utf-8")
    auth_css = AUTH_CSS.read_text(encoding="utf-8")
    assert "kira-mark-on-dark.svg" in _css_block(css, ".brand .brand-mark")
    assert "kira-mark-on-light.svg" in _css_block(
        css, ':root[data-theme="light"] .brand .brand-mark'
    )
    neon = _css_block(css, ':root[data-theme="neon"] .brand .brand-mark')
    assert "kira-mark-on-light.svg" not in neon  # neon inherits the dark-surface base mark
    assert "kira-mark-on-dark.svg" in auth_css

    for theme, (path, _digest) in BACKGROUNDS.items():
        assert f'/static/assets/{path.name}' in css, theme
    assert "url(http" not in css and "url(//" not in css and "@import" not in css
    assert "kairo-v2-bg-" not in css and "kairo-mark-" not in css


def test_bootstrap_is_external_blocking_and_runs_before_stylesheets() -> None:
    html = INDEX.read_text(encoding="utf-8")
    boot_src = '/static/ui/boot.js'
    preloader_href = '/static/kira-preloader.css'
    app_href = '/static/kira.css'
    assert html.index(boot_src) < html.index(preloader_href) < html.index(app_href)

    boot_tag = re.search(rf"<script\b[^>]*src=[\"']{re.escape(boot_src)}[\"'][^>]*>", html)
    assert boot_tag is not None
    assert not re.search(r"\b(?:async|defer)\b|type=[\"']module[\"']", boot_tag.group(0))
    scripts = re.findall(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>", html, re.S | re.I)
    assert scripts and all(
        re.search(r"\bsrc\s*=", attrs) and not body.strip() for attrs, body in scripts
    )
    assert not re.search(r"<style\b|\sstyle\s*=|\son[a-z]+\s*=", html, re.I)

    for shell in (INDEX, AUTH):
        shell_html = shell.read_text(encoding="utf-8")
        assert '/static/assets/kira-favicon.svg' in shell_html
        assert "kairo-favicon.svg" not in shell_html


def test_loader_markup_is_first_hidden_decorative_and_noninteractive() -> None:
    html = INDEX.read_text(encoding="utf-8")
    parsed = _LoaderMarkup()
    parsed.feed(html)
    assert parsed.first_body_tag is not None
    _tag, first_attrs = parsed.first_body_tag
    assert "kira-preloader" in (first_attrs.get("class") or "").split()
    assert "hidden" in parsed.loader_attrs
    assert parsed.loader_attrs.get("aria-hidden") == "true"
    assert parsed.loader_attrs.get("aria-modal") is None
    assert parsed.focusable_tags == []
    assert "dialog" not in parsed.loader_roles
    assert parsed.classes and all(name.startswith("kira-preloader") for name in parsed.classes)
    assert "inert" not in html.lower()

    clip_ids = re.findall(r'\bid=["\'](kira-preloader-clip-[^"\']+)', html)
    assert len(clip_ids) == 4 and len(set(clip_ids)) == 4
    assert html.count("<path") == 1, "the supplied monogram path should be defined once"
    assert html.count("<use") >= 5, "clipped letters and final mark should reuse one path"


def test_loader_css_is_namespaced_default_hidden_and_never_captures_input() -> None:
    css = PRELOADER_CSS.read_text(encoding="utf-8")
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    base = _css_block(without_comments, ".kira-preloader")
    assert re.search(r"--kira-preloader-ready\s*:\s*1", base)
    assert re.search(r"pointer-events\s*:\s*none", base)
    assert re.search(r"(?:opacity\s*:\s*0|visibility\s*:\s*hidden|display\s*:\s*none)", base)
    assert '[data-kira-boot="pending"]' in without_comments
    assert "prefers-reduced-motion: reduce" in without_comments
    assert re.search(r"prefers-reduced-motion:\s*reduce.*?display:\s*none", without_comments, re.S)

    assert not re.search(r"(^|[},])\s*body\b", without_comments, re.M)
    assert not re.search(r"(^|[,{])\s*\*\s*(?=[,{:])", without_comments, re.M)
    assert not re.search(
        r"(?<![\w-])\.(?:stage|logo|page|letter|loaded|loader)(?![\w-])", without_comments
    )
    keyframes = re.findall(r"@keyframes\s+([\w-]+)", without_comments)
    assert keyframes and all(name.startswith("kira-preloader-") for name in keyframes)


def test_boot_contract_is_canonical_first_reduced_motion_once_per_tab_and_fail_open() -> None:
    boot = BOOT.read_text(encoding="utf-8")
    canonical = boot.index("kira:appearance")
    legacy = boot.index("kairo:appearance")
    assert canonical < legacy
    assert "kira:preloader-seen:v1" in boot
    assert "kira:app-ready" in boot
    assert re.search(
        r"matchMedia\(\s*['\"]\(prefers-reduced-motion:\s*reduce\)['\"]\s*\)", boot
    )
    assert re.search(r"\b(?:3_?000|3000)\b", boot)
    assert re.search(r"\b120\b", boot)
    assert "--kira-preloader-ready" in boot
    for state in ("pending", "ready", "failed-open", "skipped"):
        assert state in boot
    for value in ("noir", "light", "neon", "comfortable", "compact", "focused", "expanded"):
        assert value in boot

    lowered = boot.lower()
    assert "4250" not in boot
    assert "window.onload" not in lowered
    assert not re.search(r"addEventListener\(\s*['\"]load['\"]", boot)
    assert "document.body.style" not in boot and "overflow" not in lowered
    assert "inert" not in lowered
    assert not re.search(r"\.startswith\(\s*['\"]kairo:appearance", lowered)


def test_application_has_a_single_named_startup_ready_event_source() -> None:
    app = APP.read_text(encoding="utf-8")
    assert len(re.findall(r"new Event\(\s*['\"]kira:app-ready['\"]\s*\)", app)) == 1
    assert "kira:app-ready" not in INDEX.read_text(encoding="utf-8")
