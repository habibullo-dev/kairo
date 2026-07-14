// Appearance layer (Phase 11 T5). Client-side ONLY — persisted to localStorage, applied to
// <html> (documentElement) so <body> stays plain. There is deliberately NO server theme route
// (that would be new authority). Themes: noir (default, dark), light, neon.
import { emit as busEmit } from "./bus.js";
import { readMigrated, writeStored } from "./storage.js";

const KEY = "kira:appearance";
const LEGACY_KEYS = ["kairo:appearance"];
export const THEMES = ["noir", "light", "neon"];
const DEFAULTS = { theme: "noir", density: "comfortable", layout: "focused", motion: "on", accent: "" };

let state = { ...DEFAULTS };

function load() {
  try {
    return { ...DEFAULTS, ...(JSON.parse(readMigrated("local", KEY, LEGACY_KEYS) || "{}") || {}) };
  } catch {
    return { ...DEFAULTS };
  }
}

function save() {
  writeStored("local", KEY, JSON.stringify(state));
}

function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
}

// Push the current state onto <html>: data-theme / data-density / data-layout, the reduce-motion
// class, and an optional accent override (--accent + --accent-rgb).
export function apply() {
  const r = document.documentElement;
  r.dataset.theme = state.theme;
  r.dataset.density = state.density;
  r.dataset.layout = state.layout;
  r.classList.toggle("reduce-motion", state.motion === "off");
  if (state.accent && hexToRgb(state.accent)) {
    r.style.setProperty("--accent", state.accent);
    r.style.setProperty("--accent-rgb", hexToRgb(state.accent));
  } else {
    r.style.removeProperty("--accent");
    r.style.removeProperty("--accent-rgb");
  }
}

export function get() {
  return { ...state };
}

export function set(patch) {
  state = { ...state, ...patch };
  save();
  apply();
  // Notify any appearance indicator (the status-bar toggle AND the Settings screen) so two
  // controls for the same state never disagree about the active theme. initTheme() applies
  // without emitting — this fires only on a user-driven change.
  busEmit("appearance", { ...state });
}

export function setTheme(theme) {
  if (THEMES.includes(theme)) set({ theme });
}

export function cycleTheme() {
  const i = THEMES.indexOf(state.theme);
  setTheme(THEMES[(i + 1) % THEMES.length]);
}

// Called once at startup (app.js init). Loads persisted appearance and applies it.
export function initTheme() {
  state = load();
  apply();
}
