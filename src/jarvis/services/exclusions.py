"""Sensitive-path exclusions for the local scanners (amendment B4).

Two belts, both derived from the ONE source of truth — :mod:`jarvis.paths`:

1. **Exclusion args** passed to the scanner CLI (``--exclude`` globs) so it never opens a
   secret/credential/token file in the first place.
2. **Output filter**: every finding's path is re-checked with :func:`jarvis.paths.is_sensitive_path`
   as a second belt, so a finding that names a sensitive file (e.g. the scanner ignored an
   exclude, or a rule matched inside an allowed file that resolves sensitive) is dropped before
   any model sees it.

Deriving from ``paths.py`` (not a hand-kept list) means the floor can only ever be *tightened*
by editing the one place every read/write already respects.
"""

from __future__ import annotations

from pathlib import Path

from jarvis.paths import _SENSITIVE_DIRS, _SENSITIVE_PATTERNS, is_sensitive_path, resolve_path


#: Glob patterns handed to a scanner's ``--exclude`` so it skips secret/credential/token files.
#: Derived from the sensitive floor: the always-denied filename/path patterns plus the
#: all-secret directories and the Kira token store (``data/connectors/``).
def exclude_globs() -> list[str]:
    globs: list[str] = []
    for pat in _SENSITIVE_PATTERNS:
        # scanners take path globs; strip the leading "*/" the fnmatch floor uses for depth.
        globs.append(pat[2:] if pat.startswith("*/") else pat)
    for d in sorted(_SENSITIVE_DIRS):
        globs.append(d)
        globs.append(f"{d}/*")
    globs.append("data/connectors")  # the on-disk OAuth/refresh token store
    globs.append("data/connectors/*")
    # de-dup, stable order
    seen: set[str] = set()
    out: list[str] = []
    for g in globs:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def finding_is_sensitive(finding_path: str, root: Path) -> bool:
    """Second belt: True if a finding names a path on the sensitive floor (⇒ drop the finding).
    Resolved against ``root`` exactly as the gate/tools resolve, so relative and ``..`` paths
    can't smuggle a sensitive file past the check."""
    if not finding_path:
        return False
    try:
        resolved = resolve_path(finding_path, root)
    except (OSError, ValueError):
        return True  # unresolvable ⇒ err toward dropping it
    return is_sensitive_path(resolved)
