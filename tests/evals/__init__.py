"""Live evaluation harness (Phase 5).

Importable as ``tests.evals.*`` (repo root is put on ``sys.path`` by the root
conftest). The runner is invoked as ``python -m tests.evals.runner``; the sibling
modules (recorder, judge, report, retrieval) are imported both by it and by the
keyless unit tests under ``tests/unit/``.
"""
