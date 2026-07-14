"""Structural safety pins for canonical-first browser storage migration."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from jarvis.ui.server import STATIC_DIR

STORAGE = (STATIC_DIR / "ui" / "storage.js").read_text(encoding="utf-8")
THEME = (STATIC_DIR / "ui" / "theme.js").read_text(encoding="utf-8")
VOICE = (STATIC_DIR / "ui" / "voice.js").read_text(encoding="utf-8")
MEETINGS = (STATIC_DIR / "screens" / "meetings.js").read_text(encoding="utf-8")
GRAPH = (STATIC_DIR / "screens" / "workspace" / "graph.js").read_text(encoding="utf-8")
MEMORY = (STATIC_DIR / "screens" / "workspace" / "memory.js").read_text(encoding="utf-8")
OFFICE = (STATIC_DIR / "screens" / "workspace" / "office.js").read_text(encoding="utf-8")


def test_migration_reads_canonical_first_and_retains_rollback_alias() -> None:
    canonical_read = STORAGE.index("storage.getItem(canonicalKey)")
    legacy_loop = STORAGE.index("for (const legacyKey of legacyKeys)")
    copy = STORAGE.index("storage.setItem(canonicalKey, legacy)")
    assert canonical_read < legacy_loop < copy
    assert "Keep the old value during the compatibility window" in STORAGE


def test_storage_failures_are_non_fatal_and_exact_value_cleanup_is_available() -> None:
    assert "export function readMigrated" in STORAGE
    assert "export function readStored" in STORAGE
    assert "export function writeStored" in STORAGE
    assert "export function removeStored" in STORAGE
    assert "export function removeStoredIfValue" in STORAGE
    assert "storage.getItem(key) === expected" in STORAGE
    assert "return null" in STORAGE and "return false" in STORAGE


def test_every_live_preference_writes_a_kira_key_and_keeps_only_a_read_alias() -> None:
    pairs = (
        (THEME, "kira:appearance", "kairo:appearance"),
        (VOICE, "kira:voice:playback", "kairo:voice:playback"),
        (GRAPH, "kira:graph:v4:", "kairo:graph:v4:"),
        (GRAPH, "kira:graph:focus:", "kairo:graph:focus:"),
        (OFFICE, "kira:office:", "kairo:office:"),
    )
    for source, canonical, legacy in pairs:
        assert canonical in source
        assert legacy in source
        assert "readMigrated" in source
    assert "kira:meeting-capture:" in MEETINGS
    assert "kairo:meeting-capture:" in MEETINGS
    assert "readStored" in MEETINGS
    assert "kira:graph:v4:" in MEMORY and "kairo:graph:" not in MEMORY


def test_meeting_receipt_migrates_exact_uuid_and_clears_both_names_conditionally() -> None:
    assert 'readStored("session", keys.key)' in MEETINGS
    assert 'readStored("session", keys.legacyKey)' in MEETINGS
    assert "CAPTURE_ID.test(canonical" in MEETINGS
    assert 'writeStored("session", keys.key, id)' in MEETINGS
    assert 'writeStored("session", keys.legacyKey, id)' in MEETINGS
    exact_clear = 'removeStoredIfValue("session", [receipt.key, receipt.legacyKey], receipt.id)'
    assert exact_clear in MEETINGS


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is unavailable")
def test_storage_helper_runtime_precedence_copy_failure_and_exact_cleanup() -> None:
    assert NODE is not None
    module_uri = (STATIC_DIR / "ui" / "storage.js").resolve().as_uri()
    script = f"""
const m = await import({json.dumps(module_uri)});
const local = new Map([['kairo:only', 'legacy']]);
globalThis.localStorage = {{
  getItem: key => local.has(key) ? local.get(key) : null,
  setItem: (key, value) => local.set(key, value),
  removeItem: key => local.delete(key),
}};
if (m.readMigrated('local', 'kira:only', ['kairo:only']) !== 'legacy') throw Error('read');
if (local.get('kira:only') !== 'legacy' || local.get('kairo:only') !== 'legacy')
  throw Error('copy');
local.set('kira:only', 'canonical');
local.set('kairo:only', 'stale');
if (m.readMigrated('local', 'kira:only', ['kairo:only']) !== 'canonical') throw Error('precedence');
const session = new Map([['kairo:blocked', 'receipt']]);
globalThis.sessionStorage = {{
  getItem: key => session.has(key) ? session.get(key) : null,
  setItem: () => {{ throw Error('quota'); }},
  removeItem: key => session.delete(key),
}};
if (m.readMigrated('session', 'kira:blocked', ['kairo:blocked']) !== 'receipt')
  throw Error('fallback');
if (!session.has('kairo:blocked')) throw Error('destructive failure');
globalThis.sessionStorage.setItem = (key, value) => session.set(key, value);
session.set('kira:receipt', 'A');
session.set('kairo:receipt', 'B');
m.removeStoredIfValue('session', ['kira:receipt', 'kairo:receipt'], 'A');
if (session.has('kira:receipt') || session.get('kairo:receipt') !== 'B') throw Error('exact clear');
"""
    subprocess.run(
        [NODE, "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )
