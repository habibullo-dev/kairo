"""Tests for kira.paths: unified resolution + the sensitive-path safety floor."""

from __future__ import annotations

from pathlib import Path

from kira.paths import (
    is_safe_to_persist_dir,
    is_sensitive_path,
    matches_any,
    resolve_path,
)

# --- resolve_path ----------------------------------------------------------


def test_relative_resolves_under_root(tmp_path: Path) -> None:
    assert resolve_path("notes/todo.txt", tmp_path) == (tmp_path / "notes" / "todo.txt").resolve()


def test_absolute_ignores_root(tmp_path: Path) -> None:
    other = tmp_path.parent / "elsewhere.txt"
    assert resolve_path(str(other), tmp_path) == other.resolve()


def test_dotdot_is_collapsed(tmp_path: Path) -> None:
    # ".." can't be used to escape then re-enter unpredictably — it's normalized.
    assert resolve_path("a/../b.txt", tmp_path) == (tmp_path / "b.txt").resolve()


def test_resolve_accepts_path_objects(tmp_path: Path) -> None:
    assert resolve_path(Path("x.txt"), tmp_path) == (tmp_path / "x.txt").resolve()


# --- is_sensitive_path -----------------------------------------------------


def test_env_file_is_sensitive(tmp_path: Path) -> None:
    assert is_sensitive_path(tmp_path / ".env")
    assert is_sensitive_path(tmp_path / ".env.local")
    assert is_sensitive_path(tmp_path / ".env.production")


def test_env_templates_are_not_sensitive(tmp_path: Path) -> None:
    # Committed templates carry no real secrets — the agent may read them.
    assert not is_sensitive_path(tmp_path / ".env.example")
    assert not is_sensitive_path(tmp_path / ".env.sample")
    assert not is_sensitive_path(tmp_path / ".env.template")


def test_ssh_and_keys_are_sensitive(tmp_path: Path) -> None:
    assert is_sensitive_path(tmp_path / ".ssh" / "id_rsa")
    assert is_sensitive_path(tmp_path / ".ssh" / "known_hosts")
    assert is_sensitive_path(tmp_path / "server.pem")
    assert is_sensitive_path(tmp_path / "cert.key")


def test_cloud_and_token_files_are_sensitive(tmp_path: Path) -> None:
    assert is_sensitive_path(tmp_path / ".aws" / "credentials")
    assert is_sensitive_path(tmp_path / ".git-credentials")
    assert is_sensitive_path(tmp_path / ".npmrc")
    assert is_sensitive_path(tmp_path / ".kube" / "config")


def test_ordinary_files_are_not_sensitive(tmp_path: Path) -> None:
    assert not is_sensitive_path(tmp_path / "notes.txt")
    assert not is_sensitive_path(tmp_path / "src" / "main.py")
    assert not is_sensitive_path(tmp_path / "README.md")


def test_connector_token_files_are_sensitive(tmp_path: Path) -> None:
    # Phase 9: OAuth/refresh tokens live under data/connectors/ — a durable credential the
    # agent must never read/list/exfil. Guarded by a path pattern (not a dir-component set).
    assert is_sensitive_path(tmp_path / "data" / "connectors" / "google_token.json")
    assert is_sensitive_path(tmp_path / "data" / "connectors" / "kakao_token.json")


def test_connector_source_package_is_not_sensitive(tmp_path: Path) -> None:
    # The regression the *pattern* (not a _SENSITIVE_DIRS "connectors" entry) exists to avoid:
    # the source package src/kira/connectors/*.py must stay readable. A component-match set
    # would have blocked it because the path also contains a "connectors" component.
    assert not is_sensitive_path(tmp_path / "src" / "kira" / "connectors" / "base.py")
    assert not is_sensitive_path(tmp_path / "src" / "kira" / "connectors" / "google" / "gmail.py")


def test_matching_is_case_insensitive(tmp_path: Path) -> None:
    assert is_sensitive_path(tmp_path / "SERVER.PEM")


# --- matches_any -----------------------------------------------------------


def test_matches_any_spans_directories(tmp_path: Path) -> None:
    assert matches_any(tmp_path / "a" / "b" / "secret.txt", ["*/secret.txt"])
    assert not matches_any(tmp_path / "a" / "public.txt", ["*/secret.txt"])


# --- is_safe_to_persist_dir ------------------------------------------------


def test_normal_dir_is_safe_to_persist(tmp_path: Path) -> None:
    assert is_safe_to_persist_dir(tmp_path / "exports")


def test_drive_root_is_not_safe_to_persist(tmp_path: Path) -> None:
    assert not is_safe_to_persist_dir(Path(tmp_path.anchor))


def test_home_dir_is_not_safe_to_persist() -> None:
    assert not is_safe_to_persist_dir(Path.home())


def test_sensitive_dir_is_not_safe_to_persist(tmp_path: Path) -> None:
    assert not is_safe_to_persist_dir(tmp_path / ".ssh")
