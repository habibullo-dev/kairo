"""Pure, deterministic extraction of same-project source import relationships."""

from __future__ import annotations

from kira.graph.code_dependencies import SourceHead, local_import_pairs


def test_python_imports_resolve_from_the_uploaded_source_root() -> None:
    pairs = local_import_pairs(
        [
            SourceHead(1, "repo/src/kira/app.py", "from .core import runner\nimport kira.util"),
            SourceHead(2, "repo/src/kira/core.py", "def runner(): pass"),
            SourceHead(3, "repo/src/kira/util.py", "pass"),
        ]
    )
    assert pairs == [(1, 2), (1, 3)]


def test_relative_js_imports_resolve_but_packages_and_escape_paths_do_not() -> None:
    pairs = local_import_pairs(
        [
            SourceHead(
                1,
                "repo/web/src/main.ts",
                'import { helper } from "./helper"; import react from "react"; '
                'import bad from "../../outside";',
            ),
            SourceHead(2, "repo/web/src/helper.ts", "export const helper = 1;"),
        ]
    )
    assert pairs == [(1, 2)]


def test_untrusted_source_text_is_never_evaluated_or_guessed() -> None:
    pairs = local_import_pairs(
        [
            SourceHead(
                1, "repo/src/a.py", "__import__(user_input)\nfrom does.not.exist import nope"
            ),
            SourceHead(2, "repo/src/b.py", "pass"),
        ]
    )
    assert pairs == []
