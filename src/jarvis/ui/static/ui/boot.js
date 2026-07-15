// Kira's pre-paint appearance and startup handoff. This classic head script intentionally has
// no imports: it must apply a validated saved theme before styles paint, then fail open even if
// the module application never starts.
(() => {
  "use strict";

  const root = document.documentElement;
  const appearanceKeys = ["kira:appearance", "kairo:appearance"];
  const seenKey = "kira:preloader-seen:v1";
  const allowed = {
    theme: new Set(["noir", "light", "neon"]),
    density: new Set(["comfortable", "compact"]),
    layout: new Set(["focused", "expanded"]),
    motion: new Set(["on", "off"]),
  };
  const defaults = {
    theme: "noir", density: "comfortable", layout: "focused", motion: "on", accent: "",
  };

  function storedAppearance() {
    try {
      let raw = null;
      for (const key of appearanceKeys) {
        raw = localStorage.getItem(key);
        if (raw !== null) break;
      }
      const parsed = raw === null ? null : JSON.parse(raw);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch {
      return {};
    }
  }

  function normalizeAppearance(candidate) {
    const appearance = { ...defaults };
    for (const field of ["theme", "density", "layout", "motion"]) {
      if (allowed[field].has(candidate[field])) appearance[field] = candidate[field];
    }
    if (typeof candidate.accent === "string" && /^#[0-9a-f]{6}$/i.test(candidate.accent)) {
      appearance.accent = candidate.accent;
    }
    return appearance;
  }

  function applyAppearance(appearance) {
    root.dataset.theme = appearance.theme;
    root.dataset.density = appearance.density;
    root.dataset.layout = appearance.layout;
    root.classList.toggle("reduce-motion", appearance.motion === "off");
    if (appearance.accent) {
      const value = Number.parseInt(appearance.accent.slice(1), 16);
      root.style.setProperty("--accent", appearance.accent);
      root.style.setProperty(
        "--accent-rgb",
        `${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}`,
      );
    } else {
      root.style.removeProperty("--accent");
      root.style.removeProperty("--accent-rgb");
    }
  }

  const appearance = normalizeAppearance(storedAppearance());
  applyAppearance(appearance);

  let reducedMotion = appearance.motion === "off";
  try {
    reducedMotion ||= window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    // An unavailable media-query API is not itself a reason to block startup.
  }
  if (reducedMotion) {
    root.dataset.kiraBoot = "skipped";
    return;
  }

  try {
    if (sessionStorage.getItem(seenKey) === "1") {
      root.dataset.kiraBoot = "skipped";
      return;
    }
    // Mark the attempt before revealing. A reload during a failed startup must never replay an
    // overlay or turn a decorative brand moment into a recovery obstacle.
    sessionStorage.setItem(seenKey, "1");
  } catch {
    root.dataset.kiraBoot = "skipped";
    return;
  }

  let finished = false;
  let revealTimer = null;
  let hideTimer = null;
  root.dataset.kiraBoot = "armed";

  function loader() {
    return document.getElementById("kira-preloader");
  }

  function hideAfterTransition() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      const element = loader();
      if (element) element.hidden = true;
    }, 180);
  }

  function finish(state) {
    if (finished) return;
    finished = true;
    clearTimeout(revealTimer);
    clearTimeout(failOpenTimer);
    document.removeEventListener("kira:app-ready", onReady);
    root.dataset.kiraBoot = state;
    hideAfterTransition();
  }

  function onReady() {
    finish("ready");
  }

  function scheduleReveal() {
    revealTimer = setTimeout(() => {
      if (finished) return;
      const element = loader();
      if (!element) {
        finish("failed-open");
        return;
      }
      const style = getComputedStyle(element);
      const cssReady = style.getPropertyValue("--kira-preloader-ready").trim() === "1"
        && style.position === "fixed" && style.pointerEvents === "none";
      if (!cssReady) {
        // The markup is deliberately hidden without its component stylesheet. Never expose a
        // raw full-page SVG if CSS was blocked, missing, or replaced by an incomplete response.
        finish("failed-open");
        return;
      }
      element.hidden = false;
      root.dataset.kiraBoot = "pending";
    }, 120);
  }

  document.addEventListener("kira:app-ready", onReady, { once: true });
  const failOpenTimer = setTimeout(() => finish("failed-open"), 3000);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scheduleReveal, { once: true });
  } else {
    scheduleReveal();
  }
})();
