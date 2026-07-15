"""Scaffold smoke test: the package imports and exposes a version.

Kept deliberately trivial so `pytest` is green from task 1 with no API key.
Real unit tests arrive with their subsystems (tools, gate, loop).
"""

import kira


def test_package_importable() -> None:
    assert kira.__version__ == "0.1.0"
