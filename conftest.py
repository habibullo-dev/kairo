"""Pytest root conftest.

Puts the repo root on ``sys.path`` so tests can import the eval infrastructure at
``tests.evals.*`` (a namespace package) alongside the pip-installed ``jarvis``
package. The eval modules live under ``tests/`` — not shipped in ``src/`` — because
they are test infrastructure, and the runner is invoked as ``python -m
tests.evals.runner``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
