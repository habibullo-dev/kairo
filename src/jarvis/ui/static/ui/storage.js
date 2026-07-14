// Browser-only persistence helpers for brand-key migrations. Storage is convenience state, never
// authority: every caller validates/clamps the returned value before use and the server remains
// authoritative for sessions, workspaces, and mutations.

function storageFor(kind) {
  try {
    if (kind === "local") return globalThis.localStorage || null;
    if (kind === "session") return globalThis.sessionStorage || null;
  } catch { /* blocked by browser policy */ }
  return null;
}

export function readMigrated(kind, canonicalKey, legacyKeys = []) {
  const storage = storageFor(kind);
  if (!storage) return null;
  try {
    const canonical = storage.getItem(canonicalKey);
    if (canonical !== null) return canonical;
  } catch {
    return null;
  }
  for (const legacyKey of legacyKeys) {
    let legacy;
    try { legacy = storage.getItem(legacyKey); } catch { return null; }
    if (legacy === null) continue;
    try {
      // Keep the old value during the compatibility window. That makes rollback/cached old tabs
      // degrade to their last known preference instead of silently losing it. Canonical-first
      // reads ensure the alias can never override newer Kira state.
      storage.setItem(canonicalKey, legacy);
    } catch { /* use the legacy value in memory and retry on a later load */ }
    return legacy;
  }
  return null;
}

export function readStored(kind, key) {
  const storage = storageFor(kind);
  if (!storage) return null;
  try { return storage.getItem(key); } catch { return null; }
}

export function writeStored(kind, key, value) {
  const storage = storageFor(kind);
  if (!storage) return false;
  try {
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function removeStored(kind, keys) {
  const storage = storageFor(kind);
  if (!storage) return false;
  let removed = true;
  for (const key of keys) {
    try { storage.removeItem(key); } catch { removed = false; }
  }
  return removed;
}

export function removeStoredIfValue(kind, keys, expected) {
  const storage = storageFor(kind);
  if (!storage) return false;
  let removed = true;
  for (const key of keys) {
    try {
      if (storage.getItem(key) === expected) storage.removeItem(key);
    } catch { removed = false; }
  }
  return removed;
}
