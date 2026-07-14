"""All HTML-interpolating screens use the shared text escaper."""

from jarvis.ui.server import STATIC_DIR


def test_no_screen_keeps_a_local_escaper_or_imports_one_from_vault() -> None:
    for name in ("vault.js", "tasks.js", "lab.js", "memory.js"):
        source = (STATIC_DIR / "screens" / name).read_text(encoding="utf-8")
        assert "function esc(" not in source
        assert 'from "./vault.js"' not in source
    for name in ("tasks.js", "lab.js"):
        source = (STATIC_DIR / "screens" / name).read_text(encoding="utf-8")
        # The module may import other shared DOM helpers alongside esc.
        assert 'esc } from "../ui/dom.js"' in source


def test_vault_uses_csp_safe_classes_and_attribute_escaping() -> None:
    source = (STATIC_DIR / "screens" / "vault.js").read_text(encoding="utf-8")
    assert 'import { esc, escAttr } from "../ui/dom.js"' in source
    assert 'href="#workspace/${escAttr(String(readiness.project_id))}/graph"' in source
    assert 'style="' not in source
