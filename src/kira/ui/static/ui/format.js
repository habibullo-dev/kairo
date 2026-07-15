// Shared formatters (Phase 11 T5). Screens import these instead of redefining inline closures.

// Cost formatter. null/undefined -> "—": an UNPRICED value is shown distinctly, NEVER summed or
// rendered as $0 (matches the ledger's fail-closed contract). Small values keep 4 decimals.
export function money(n) {
  if (n == null) return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  return "$" + (Math.abs(v) < 1 ? v.toFixed(4) : v.toFixed(2));
}

// Compact relative time from a UTC ISO string, for calm timestamps ("3m", "2h", "5d").
export function relTime(iso) {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const secs = Math.max(0, (Date.now() - t) / 1000);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

// Human byte size.
export function bytes(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(v) / Math.log(1024)));
  return `${(v / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}
