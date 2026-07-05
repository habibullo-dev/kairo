"""Shared path resolution and sensitive-path detection.

This is foundation code (no dependencies on the rest of Jarvis) used by *both*
the :class:`~jarvis.permissions.gate.PermissionGate` and the filesystem tools.
Putting it here removes a subtle safety hole: if the gate resolved a relative
path one way (against the project root) and the tool resolved it another (against
the process CWD), the gate could approve one file while the tool touched another.
Everyone resolves through :func:`resolve_path` so the decision and the side effect
refer to the *same* absolute path.

:func:`is_sensitive_path` is a non-negotiable safety floor: a hard-coded set of
secret/credential patterns that are denied regardless of policy, so no edit to
``permissions.yaml`` can accidentally expose them. Policy can *add* patterns
(``filesystem.read_denylist``) but never remove this floor.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path

# --- resolution ------------------------------------------------------------


def resolve_path(raw: str | Path, root: Path) -> Path:
    """Resolve ``raw`` to an absolute, normalized path.

    Relative paths are taken against ``root`` (the workspace/project root), never
    the process CWD — this is what keeps the gate's decision and the tool's action
    pointed at the same file. ``resolve()`` also collapses ``..`` and follows
    symlinks, so an allowlist can't be escaped via ``notes/../../etc`` or a symlink
    pointing outside it.
    """
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def matches_any(path: Path, patterns: list[str]) -> bool:
    """True if the path's posix form matches any fnmatch pattern (case-insensitive).

    ``*`` deliberately spans ``/`` here (fnmatch semantics), so ``*.env`` matches a
    ``.env`` at any depth. For a denylist, broad matching errs toward *safe*.
    """
    target = path.as_posix().lower()
    return any(fnmatchcase(target, pat.lower()) for pat in patterns)


# --- sensitive-path floor --------------------------------------------------

# Directories that are essentially all-secret: if any path component matches one
# of these (case-insensitively), the path is sensitive. Component matching also
# catches the directory itself (e.g. a bare ``.ssh``), not just files under it.
_SENSITIVE_DIRS: frozenset[str] = frozenset({".ssh", ".aws", ".gnupg", ".kube"})

# Filename / full-path glob patterns that are ALWAYS denied for read and write.
# Matched against the resolved posix path, case-insensitively, via matches_any.
_SENSITIVE_PATTERNS: list[str] = [
    "*.env",  # .env, foo.env — real secrets
    "*/.env.*",  # .env.local, .env.production (templates excepted below)
    "*/id_rsa",
    "*/id_rsa.*",
    "*/id_dsa",
    "*/id_ecdsa",
    "*/id_ed25519",
    "*/id_ed25519.*",
    "*.pem",
    "*.key",
    "*.ppk",
    "*.pfx",
    "*.p12",
    "*.keystore",
    "*/.docker/config.json",
    "*/.netrc",
    "*/.git-credentials",
    "*/.npmrc",
    "*/.pypirc",
    "*/credentials.json",
    "*secrets.json",
    "*secrets.yaml",
    "*secrets.yml",
]

# Env files that are safe by construction (committed templates, no real secrets).
# Checked by exact filename so a real ".env.production" is never excepted.
_SAFE_ENV_TEMPLATES: frozenset[str] = frozenset(
    {".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults"}
)


def is_sensitive_path(path: Path) -> bool:
    """True if ``path`` looks like a secret/credential the agent must never touch.

    A hard safety floor, independent of policy. Errs toward denying: a committed
    template like ``.env.example`` is the one explicit exception (harmless, and
    the agent legitimately needs to read it).
    """
    if path.name.lower() in _SAFE_ENV_TEMPLATES:
        return False
    if {part.lower() for part in path.parts} & _SENSITIVE_DIRS:
        return True
    return matches_any(path, _SENSITIVE_PATTERNS)


# --- persistence safety ----------------------------------------------------


def is_safe_to_persist_dir(path: Path) -> bool:
    """Guard for "always allow" on a write: refuse to persist an over-broad grant.

    Persisting a filesystem root (``C:\\``, ``/``) or the home directory itself
    into the write allowlist would silently authorize sweeping future writes from
    a single approval. Sensitive directories are refused for the same reason. The
    one approved write still proceeds; only the *persisted* broadening is blocked.
    """
    p = path.resolve()
    if p == Path(p.anchor):  # a drive/filesystem root
        return False
    try:
        if p == Path.home().resolve():
            return False
    except (RuntimeError, OSError):
        pass  # home not resolvable in this environment — fall through
    return not is_sensitive_path(p)
