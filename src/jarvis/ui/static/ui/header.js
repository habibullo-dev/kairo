// Conversation header (Phase 15.5) — the calm control bar above the chat. It answers, at a glance:
// which project (if any), which chat, on which model, in which mode, and what is
// available. Every value is real server state (the runner / models / capabilities / projects read
// models); the controls POST only to the enumerated UI-state routes (projects-select, model,
// effort, mode) — never the agent-turn or approval routes. The chat shelf lives at the page edge,
// not in this compact composer control. All text
// is set via el()/textContent (chat titles + project names are user/model text) — no raw-HTML sink.
import { el } from "./dom.js";
import { showToast } from "./feedback.js";

const MODES = [["plan", "Planning"], ["approval", "Approval"], ["auto", "Auto"]];

let _host = null;
let _api = null;
let _onChanged = () => {};
let _refreshRevision = 0;
let _latestHeaderRefresh = null;

// Header renders replace every selector, so operation ownership cannot live on a DOM node. Keep
// one operation per logical setting and bind it to the router's authenticated authority instead.
// Effort keys include their model because effort is persisted per model server-side.
const controlOperations = new Map();

function authorityToken(api = _api) {
  return typeof api?.authorityToken === "function" ? api.authorityToken() : null;
}

function authorityIsCurrent(api, token) {
  return token === null || typeof api?.authorityIsCurrent !== "function"
    || api.authorityIsCurrent(token);
}

function pendingControlOperation(setting, api = _api) {
  const operation = controlOperations.get(setting);
  const token = authorityToken(api);
  return operation && operation.authorityToken === token && authorityIsCurrent(api, token)
    ? operation : null;
}

function operationIsCurrent(operation) {
  return controlOperations.get(operation.setting) === operation
    && operation.authorityToken === authorityToken(_api)
    && authorityIsCurrent(operation.api, operation.authorityToken)
    && authorityIsCurrent(_api, operation.authorityToken);
}

function showPendingControl(control, operation) {
  if (operation.control !== control) {
    operation.control = control;
    operation.controlWasDisabled = control.disabled;
  }
  // Before the POST settles, retain the user's selected value across passive header refreshes.
  // During reconciliation, leave the freshly-read server value in place while keeping it busy.
  if (operation.phase === "posting"
      && [...control.options].some((option) => option.value === operation.pendingValue)) {
    control.value = operation.pendingValue;
  }
  control.disabled = true;
  control.setAttribute("aria-busy", "true");
  return control;
}

function restorePendingControl(control, setting) {
  const operation = pendingControlOperation(setting);
  return operation ? showPendingControl(control, operation) : control;
}

export function bindHeaderContext(host, api, opts = {}) {
  // The router keeps the same host only when workspace/session/project identity is unchanged.
  // Let an in-flight read finish across that passive rebind; a different host invalidates it.
  if (_host !== host) _refreshRevision += 1;
  _host = host;
  _api = api;
  _onChanged = opts.onChanged || (() => {});
}

export async function mountHeader(host, api, opts = {}) {
  bindHeaderContext(host, api, opts);
  await refreshHeader();
}

// Re-fetch + re-render. Safe to call any time: a no-op when the host isn't on screen (so a WS
// model/mode/project echo while another screen is open costs nothing).
export async function refreshHeader({ refreshRunner = false } = {}) {
  if (!_host || !_host.isConnected) return false;
  const revision = ++_refreshRevision;
  const host = _host;
  const api = _api;
  const refresh = { revision, promise: null };
  refresh.promise = (async () => {
    const [runner, models, caps, projects] = await Promise.all([
      api.runnerStatus({ refresh: refreshRunner }), api.get("/api/models"), api.get("/api/capabilities"),
      api.get("/api/projects"),
    ]);
    if (revision !== _refreshRevision || _host !== host || !host.isConnected
        || (typeof _api.renderIsCurrent === "function" && !_api.renderIsCurrent())) return false;
    render(runner, models, caps, projects);
    return true;
  })();
  _latestHeaderRefresh = refresh;
  return await refresh.promise;
}

// POST a UI-state change, then refresh. `conversation` also tells Daily to reload the chat view
// (a new/resumed/archived chat changed which transcript is live).
async function postControl(control, setting, path, body) {
  const api = _api;
  const existing = pendingControlOperation(setting, api);
  if (existing) {
    showPendingControl(control, existing);
    return false;
  }
  const operation = {
    api, setting, authorityToken: authorityToken(api), pendingValue: control.value,
    phase: "posting", control: null, controlWasDisabled: false,
  };
  controlOperations.set(setting, operation);
  showPendingControl(control, operation);
  let res;
  try {
    res = await api.post(path, body || {});
  } catch {
    res = { ok: false, data: { message: "Kairo could not be reached." } };
  }

  if (!operationIsCurrent(operation)) {
    if (controlOperations.get(setting) === operation) controlOperations.delete(setting);
    return false;
  }
  operation.phase = "reconciling";
  if (!res.ok) showToast(res.data?.message || "That setting was not changed.", "error");

  // If another same-authority refresh supersedes this reconciliation read, wait for that newer
  // render before releasing the selector. Otherwise a failed optimistic value could be exposed
  // briefly as writable while the authoritative replacement is still in flight.
  const requestedRevision = _refreshRevision + 1;
  let reconciled = await refreshHeader({ refreshRunner: true });
  let observedRevision = requestedRevision;
  while (!reconciled && operationIsCurrent(operation)) {
    const latest = _latestHeaderRefresh;
    if (!latest || latest.revision <= observedRevision) break;
    observedRevision = latest.revision;
    reconciled = await latest.promise;
  }

  const ownsCurrentOperation = operationIsCurrent(operation);
  if (controlOperations.get(setting) === operation) controlOperations.delete(setting);
  if (!ownsCurrentOperation) return false;
  const liveControl = operation.control;
  if (reconciled && liveControl?.isConnected) {
    liveControl.disabled = operation.controlWasDisabled;
    liveControl.removeAttribute("aria-busy");
  }
  return res.ok;
}

function scopeSelect(runner, projects, unavailable = false) {
  const setting = "project";
  const activeId = (runner.project && runner.project.id) ?? null;
  const opts = unavailable
    ? [el("option", { value: "", selected: true }, ["Project status unavailable"])]
    : [el("option", { value: "" }, ["No project"])];
  if (!unavailable) {
    for (const p of projects.projects || []) {
      opts.push(el("option", { value: String(p.id), selected: p.id === activeId },
        [p.name || `Project ${p.id}`]));
    }
  }
  const sel = el("select", {
    class: "hdr-select hdr-scope", "aria-label": "Project for this chat",
    disabled: unavailable,
    title: unavailable ? "Project status is unavailable. Reload this view to try again." : activeId == null
      ? "No project selected: this is an unassigned personal chat."
      : "Project for this chat",
  }, opts);
  sel.value = unavailable || activeId == null ? "" : String(activeId);
  restorePendingControl(sel, setting);
  sel.addEventListener("change", async () => {
    if (unavailable) return;
    // Switching scope starts a FRESH scoped conversation server-side (a session is bound to one
    // project for life). Clear the client transcript so the old chat doesn't linger.
    const ok = await postControl(sel, setting, "/api/projects/select",
      { project_id: sel.value === "" ? null : Number(sel.value) });
    if (ok) _onChanged();
  });
  return sel;
}

// Phase 15.6: the routing selector. "Auto" (recommended, default) is cost-aware per-message
// routing; below it, Manual pins a trusted Claude model. Other providers are shown DISABLED with
// an honest reason (text-only / not-allowed-for-private / unavailable). When Auto, a caption shows
// what it picked last turn ("→ Sonnet 5").
function modelSelect(models, unavailable = false) {
  const setting = "model";
  if (unavailable) {
    return restorePendingControl(el("select", { class: "hdr-select", "aria-label": "Model routing", disabled: true,
      title: "Model status is unavailable. Reload this view to try again." }, [
      el("option", { value: "" }, ["Model status unavailable"]),
    ]), setting);
  }
  const policy = models.policy || "manual";
  const auto = models.auto || {};
  const manual = models.models || [];
  const opts = [
    el("option", { value: "auto", selected: policy === "auto" },
      [`${auto.label || "Auto"} — recommended`]),
  ];
  if (manual.length) {
    opts.push(el("optgroup", { label: "Manual" }, manual.map((m) =>
      el("option", { value: m.id, selected: policy === "manual" && m.current, disabled: !m.selectable },
        [m.label]))));
  } else {
    // Never a blank picker if the manual list came back empty (a failed/absent read model).
    opts.push(el("option", { value: "", disabled: true }, ["No manual models — check providers"]));
  }
  if ((models.external || []).length) {
    opts.push(el("optgroup", { label: "Not a manual pick" },
      models.external.map((e) =>
        el("option", { value: e.id, disabled: true }, [`${e.label} — ${e.reason}`]))));
  }
  const sel = el("select", { class: "hdr-select", "aria-label": "Model routing" }, opts);
  sel.value = policy === "auto" ? "auto" : (models.current || "auto");
  sel.title = auto.description || "uses cheap models first, escalates only when needed";
  restorePendingControl(sel, setting);
  sel.addEventListener("change", () => {
    if (!unavailable && sel.value) void postControl(
      sel, setting, "/api/model", { model: sel.value },
    );
  });
  return sel;
}

// Per-model effort (cost control): lower effort ⇒ fewer output tokens ⇒ lower cost. The chosen
// level is remembered per model server-side, so switching model re-renders this with that model's
// effort. When a route manages effort itself or a model does not support it, omit the control
// entirely — a disabled "n/a" selector is visual noise, not useful information.
function effortSelect(models, unavailable = false) {
  if (unavailable) return null;
  const levels = models.effort_levels || [];
  if (!levels.length) return null;
  const cur = models.current_effort || "high";
  const setting = `effort:${models.current || ""}`;
  const curRow = (models.models || []).find((m) => m.current);
  // Auto manages effort per tier. Haiku-like economy models do not accept an effort parameter.
  const isAuto = (models.policy || "manual") === "auto";
  const supported = !isAuto && (!curRow || curRow.supports_effort !== false);
  if (!supported) return null;
  const opts = levels.map((lv) => el("option", { value: lv.id, selected: lv.id === cur }, [lv.label]));
  const sel = el("select", { class: "hdr-select hdr-effort", "aria-label": "Effort (cost)" }, opts);
  sel.value = cur;
  sel.title = "Lower effort spends fewer tokens (cheaper); higher is more thorough.";
  restorePendingControl(sel, setting);
  sel.addEventListener("change", () => {
    if (sel.value) void postControl(
      sel, setting, "/api/effort", { effort: sel.value, model: models.current },
    );
  });
  return sel;
}

function modeSelect(runner, unavailable = false) {
  const setting = "mode";
  if (unavailable) {
    return restorePendingControl(el("select", { class: "hdr-select", "aria-label": "Mode", disabled: true,
      title: "Run-mode status is unavailable. Reload this view to try again." }, [
      el("option", { value: "" }, ["Mode status unavailable"]),
    ]), setting);
  }
  const cur = runner.mode || "approval";
  const sel = el("select", { class: "hdr-select", "aria-label": "Mode" },
    MODES.map(([v, label]) => el("option", { value: v, selected: v === cur }, [label])));
  sel.value = cur;
  restorePendingControl(sel, setting);
  sel.addEventListener("change", () => {
    if (!unavailable) void postControl(sel, setting, "/api/mode", { mode: sel.value });
  });
  return sel;
}

function titleCluster(runner, unavailable = false) {
  const title = unavailable ? "Chat status unavailable" : runner.session_title || "Untitled";
  return el("span", { class: "hdr-title", title }, [title]);
}

function modelMenu(runner, models, unavailable = false) {
  const policy = models.policy || "manual";
  const current = (models.models || []).find((m) => m.current);
  const model = unavailable ? "Status unavailable" : policy === "auto" ? (models.auto?.label || "Auto")
    : (current?.label || models.current || "Model");
  const effort = effortSelect(models, unavailable);
  const effortLabel = effort
    ? ((models.effort_levels || []).find((item) => item.id === models.current_effort)?.label || "")
    : "";
  const mode = unavailable ? "" : MODES.find(([id]) => id === (runner.mode || "approval"))?.[1] || "Approval";
  const summary = [model, effortLabel, mode].filter(Boolean).join(" · ");
  const fields = [
    el("label", { class: "hdr-model-field" }, [el("span", {}, ["Model"]), modelSelect(models, unavailable)]),
    ...(effort ? [el("label", { class: "hdr-model-field" }, [el("span", {}, ["Effort"]), effort])] : []),
    el("label", { class: "hdr-model-field" }, [el("span", {}, ["Mode"]), modeSelect(runner, unavailable)]),
  ];
  return el("details", { class: "hdr-model-menu" }, [
    el("summary", { "aria-label": "Model, effort, and mode", title: "Model, effort, and mode" }, [summary]),
    el("div", { class: "hdr-model-menu-items" }, fields),
  ]);
}

function render(runner, models, caps, projects) {
  const runnerUnavailable = runner == null || !!_api.state?.runnerStatusError;
  const statusUnavailable = runnerUnavailable || [models, caps, projects].some((data) => data == null);
  const scopeUnavailable = runnerUnavailable || projects == null;
  const routingUnavailable = runnerUnavailable || models == null;
  runner = runner || {};
  models = models || {};
  caps = caps || {};
  projects = projects || {};
  _host.textContent = "";
  _host.appendChild(el("div", { class: "convo-header compact" }, [
    el("div", { class: "hdr-context" }, [
      scopeSelect(runner, projects, scopeUnavailable), titleCluster(runner, runnerUnavailable),
      statusUnavailable ? el("span", {
        class: "hdr-load-warning", title: "Some chat status could not be loaded. Reload this view to try again.",
      }, ["Status unavailable"]) : null,
    ].filter(Boolean)),
    el("div", { class: "hdr-controls" }, [modelMenu(runner, models, routingUnavailable)]),
  ]));
}
