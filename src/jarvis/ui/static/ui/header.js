// Conversation header (Phase 15.5) — the calm control bar above the chat. It answers, at a glance:
// which project (if any), which chat, on which model, in which mode, and what is
// available. Every value is real server state (the runner / models / capabilities / projects read
// models); the controls POST only to the enumerated UI-state routes (projects-select, model,
// effort, mode) — never the agent-turn or approval routes. The chat shelf lives at the page edge,
// not in this compact composer control. All text
// is set via el()/textContent (chat titles + project names are user/model text) — no raw-HTML sink.
import { el } from "./dom.js";

const MODES = [["plan", "Planning"], ["approval", "Approval"], ["auto", "Auto"]];

let _host = null;
let _api = null;
let _onChanged = () => {};

export async function mountHeader(host, api, opts = {}) {
  _host = host;
  _api = api;
  _onChanged = opts.onChanged || (() => {});
  await refreshHeader();
}

// Re-fetch + re-render. Safe to call any time: a no-op when the host isn't on screen (so a WS
// model/mode/project echo while another screen is open costs nothing).
export async function refreshHeader({ refreshRunner = false } = {}) {
  if (!_host || !_host.isConnected) return;
  const [runner, models, caps, projects] = await Promise.all([
    _api.runnerStatus({ refresh: refreshRunner }), _api.get("/api/models"), _api.get("/api/capabilities"),
    _api.get("/api/projects"),
  ]);
  render(runner, models, caps, projects);
}

// POST a UI-state change, then refresh. `conversation` also tells Daily to reload the chat view
// (a new/resumed/archived chat changed which transcript is live).
async function post(path, body) {
  const res = await _api.post(path, body || {});
  if (res.ok) await refreshHeader({ refreshRunner: true });
  return res.ok;
}

function resetChat() {
  if (_api.state) {
    _api.state.chat = [];
    _api.state.chatAttachments = [];
  }
}

function scopeSelect(runner, projects, unavailable = false) {
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
  sel.addEventListener("change", async () => {
    if (unavailable) return;
    // Switching scope starts a FRESH scoped conversation server-side (a session is bound to one
    // project for life). Clear the client transcript so the old chat doesn't linger.
    const res = await _api.post("/api/projects/select",
      { project_id: sel.value === "" ? null : Number(sel.value) });
    if (res.ok) { resetChat(); await refreshHeader({ refreshRunner: true }); _onChanged(); }
  });
  return sel;
}

// Phase 15.6: the routing selector. "Auto" (recommended, default) is cost-aware per-message
// routing; below it, Manual pins a trusted Claude model. Other providers are shown DISABLED with
// an honest reason (text-only / not-allowed-for-private / unavailable). When Auto, a caption shows
// what it picked last turn ("→ Sonnet 5").
function modelSelect(models, unavailable = false) {
  if (unavailable) {
    return el("select", { class: "hdr-select", "aria-label": "Model routing", disabled: true,
      title: "Model status is unavailable. Reload this view to try again." }, [
      el("option", { value: "" }, ["Model status unavailable"]),
    ]);
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
  sel.addEventListener("change", () => { if (!unavailable && sel.value) post("/api/model", { model: sel.value }); });
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
  const curRow = (models.models || []).find((m) => m.current);
  // Auto manages effort per tier. Haiku-like economy models do not accept an effort parameter.
  const isAuto = (models.policy || "manual") === "auto";
  const supported = !isAuto && (!curRow || curRow.supports_effort !== false);
  if (!supported) return null;
  const opts = levels.map((lv) => el("option", { value: lv.id, selected: lv.id === cur }, [lv.label]));
  const sel = el("select", { class: "hdr-select hdr-effort", "aria-label": "Effort (cost)" }, opts);
  sel.value = cur;
  sel.title = "Lower effort spends fewer tokens (cheaper); higher is more thorough.";
  sel.addEventListener("change", () => {
    if (sel.value) post("/api/effort", { effort: sel.value, model: models.current });
  });
  return sel;
}

function modeSelect(runner, unavailable = false) {
  if (unavailable) {
    return el("select", { class: "hdr-select", "aria-label": "Mode", disabled: true,
      title: "Run-mode status is unavailable. Reload this view to try again." }, [
      el("option", { value: "" }, ["Mode status unavailable"]),
    ]);
  }
  const cur = runner.mode || "approval";
  const sel = el("select", { class: "hdr-select", "aria-label": "Mode" },
    MODES.map(([v, label]) => el("option", { value: v, selected: v === cur }, [label])));
  sel.value = cur;
  sel.addEventListener("change", () => { if (!unavailable) post("/api/mode", { mode: sel.value }); });
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
