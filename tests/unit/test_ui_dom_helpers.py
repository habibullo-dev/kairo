"""All HTML-interpolating screens use the shared text escaper."""

from jarvis.ui.server import STATIC_DIR


def test_no_screen_keeps_a_local_escaper_or_imports_one_from_vault() -> None:
    for name in ("vault.js", "tasks.js", "lab.js", "memory.js"):
        source = (STATIC_DIR / "screens" / name).read_text(encoding="utf-8")
        assert "function esc(" not in source
        assert 'from "./vault.js"' not in source
    for name in ("vault.js", "tasks.js", "lab.js"):
        source = (STATIC_DIR / "screens" / name).read_text(encoding="utf-8")
        assert 'import { esc } from "../ui/dom.js"' in source
