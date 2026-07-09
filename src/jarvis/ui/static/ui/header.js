// Conversation header (Phase 15.5) — the calm control bar above the chat. It answers, at a glance:
// what SCOPE am I in (Global / a project), which CHAT, on which MODEL, in which MODE, and what is
// available. Every value is real server state (the runner / models / capabilities / projects /
// sessions read models); the controls POST only to the enumerated UI-state routes (projects-select,
// sessions new|rename|archive|pin, model, effort, mode) — never the agent-turn or approval routes. All text
// is set via el()/textContent (chat titles + project names are user/model text) — no raw-HTML sink.
import { el } from "./dom.js";

const MODES = [["plan", "Planning"], ["approval", "Approval"], ["auto", "Auto"]];

let _host = null;
let _api = null;
let _onChanged = () => {};
let _renaming = false;

export async function mountHeader(host, api, opts = {}) {
  _host = host;
  _api = api;
  _onChanged = opts.onChanged || (() => {});
  await refreshHeader();
}

// Re-fetch + re-render. Safe to call any time: a no-op when the host isn't on screen (so a WS
// model/mode/project echo while another screen is open costs nothing).
export async function refreshHeader() {
  if (!_host || !_host.isConnected) return;
  const [runner, models, caps, projects, sessions] = await Promise.all([
    _api.get("/api/runner"), _api.get("/api/models"), _api.get("/api/capabilities"),
    _api.get("/api/projects"), _api.get("/api/sessions?limit=8"),
  ]);
  render(runner || {}, models || {}, caps || {}, projects || {}, sessions || {});
}

// POST a UI-state change, then refresh. `conversation` also tells Daily to reload the chat view
// (a new/resumed/archived chat changed which transcript is live).
async function post(path, body) {
  const res = await _api.post(path, body || {});
  if (res.ok) await refreshHeader();
  return res.ok;
}

function resetChat() {
  if (_api.state) _api.state.chat = [];
}

function labeled(key, control) {
  return el("label", { class: "hdr-field" }, [el("span", { class: "hdr-k" }, [key]), control]);
}

function scopeSelect(runner, projects) {
  const activeId = (runner.project && runner.project.id) ?? null;
  const opts = [el("option", { value: "" }, ["Global"])];
  for (const p of projects.projects || []) {
    opts.push(el("option", { value: String(p.id), selected: p.id === activeId },
      [p.name || `Project ${p.id}`]));
  }
  const sel = el("select", { class: "hdr-select", "aria-label": "Chat scope" }, opts);
  sel.value = activeId == null ? "" : String(activeId);
  sel.addEventListener("change", async () => {
    // Switching scope starts a FRESH scoped conversation server-side (a session is bound to one
    // project for life). Clear the client transcript so the old chat doesn't linger.
    const res = await _api.post("/api/projects/select",
      { project_id: sel.value === "" ? null : Number(sel.value) });
    if (res.ok) { resetChat(); await refreshHeader(); _onChanged(); }
  });
  return labeled("Scope", sel);
}

// Phase 15.6: the routing selector. "Auto" (recommended, default) is cost-aware per-message
// routing; below it, Manual pins a trusted Claude model. Other providers are shown DISABLED with
// an honest reason (text-only / not-allowed-for-private / unavailable). When Auto, a caption shows
// what it picked last turn ("→ Sonnet 5").
function modelSelect(models) {
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
  sel.addEventListener("change", () => { if (sel.value) post("/api/model", { model: sel.value }); });
  const field = labeled("Model", sel);
  const routed = models.routed;
  if (policy === "auto" && routed && routed.model) {
    field.appendChild(el("span", { class: "hdr-routed", title: routed.reason || "" },
      [`→ ${routed.model}`]));
  }
  return field;
}

// Per-model effort (cost control): lower effort ⇒ fewer output tokens ⇒ lower cost. The chosen
// level is remembered per model server-side, so switching model re-renders this with that model's
// effort. Degrades to nothing if the read model predates effort (never a broken control). The
// Haiku tier has NO effort knob (the API rejects it), so the control is disabled + labeled there.
function effortSelect(models) {
  const levels = models.effort_levels || [];
  if (!levels.length) return null;
  const cur = models.current_effort || "high";
  const curRow = (models.models || []).find((m) => m.current);
  // Auto manages effort per tier (client default); the manual per-model knob is disabled then.
  const isAuto = (models.policy || "manual") === "auto";
  const supported = !isAuto && (!curRow || curRow.supports_effort !== false);
  const opts = levels.map((lv) => el("option", { value: lv.id, selected: lv.id === cur }, [lv.label]));
  const disabledLabel = isAuto ? "Auto-managed" : "n/a for this model";
  if (!supported) opts.push(el("option", { value: "", selected: true }, [disabledLabel]));
  const sel = el("select",
    { class: "hdr-select", "aria-label": "Effort (cost)", disabled: !supported }, opts);
  if (supported) sel.value = cur;
  sel.title = supported
    ? "Lower effort spends fewer tokens (cheaper); higher is more thorough."
    : isAuto
      ? "Auto picks the model AND effort per turn (cheap-first). Switch to a manual model to tune effort."
      : "This economy model has no effort control (and no extended thinking) — already the cheapest tier.";
  sel.addEventListener("change", () => {
    if (sel.value && supported) post("/api/effort", { effort: sel.value, model: models.current });
  });
  return labeled("Effort", sel);
}

function modeSelect(runner) {
  const cur = runner.mode || "approval";
  const sel = el("select", { class: "hdr-select", "aria-label": "Mode" },
    MODES.map(([v, label]) => el("option", { value: v, selected: v === cur }, [label])));
  sel.value = cur;
  sel.addEventListener("change", () => post("/api/mode", { mode: sel.value }));
  return labeled("Mode", sel);
}

function titleCluster(runner) {
  const sid = runner.session_id;
  const title = runner.session_title || (sid ? `Chat ${sid}` : "New chat");
  if (_renaming && sid) {
    const input = el("input", { class: "hdr-rename", value: title, "aria-label": "Rename chat" });
    const commit = async () => {
      const v = input.value.trim();
      _renaming = false;
      if (v) await post(`/api/sessions/${sid}/rename`, { title: v });
      else await refreshHeader();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") commit();
      else if (e.key === "Escape") { _renaming = false; refreshHeader(); }
    });
    input.addEventListener("blur", commit);
    setTimeout(() => input.focus(), 0);
    return input;
  }
  return el("span", { class: "hdr-title", title }, [title]);
}

function actions(runner, sessions) {
  const sid = runner.session_id;
  const row = [];
  const newBtn = el("button", { class: "plain-button" }, ["＋ New"]);
  newBtn.addEventListener("click", async () => {
    const res = await _api.post("/api/sessions/new", {});
    if (res.ok) { resetChat(); await refreshHeader(); _onChanged(); }
  });
  row.push(newBtn);
  const chats = (sessions.sessions || []).filter((s) => s.id !== sid);
  if (chats.length) {
    const sel = el("select", { class: "hdr-select", "aria-label": "Resume a chat" },
      [el("option", { value: "" }, ["Resume…"]),
       ...chats.map((s) => el("option", { value: String(s.id) }, [s.title || `Chat ${s.id}`]))]);
    sel.addEventListener("change", async () => {
      if (!sel.value) return;
      if (await _api.resumeChat(Number(sel.value))) { await refreshHeader(); _onChanged(); }
    });
    row.push(sel);
  }
  if (sid) {
    const meta = (sessions.sessions || []).find((s) => s.id === sid);
    const pinned = !!(meta && meta.pinned);
    const rename = el("button", { class: "plain-button ghost" }, ["Rename"]);
    rename.addEventListener("click", () => { _renaming = true; refreshHeader(); });
    const pin = el("button", { class: "plain-button ghost" }, [pinned ? "Unpin" : "Pin"]);
    pin.addEventListener("click", () => post(`/api/sessions/${sid}/pin`, { pinned: !pinned }));
    const arch = el("button", { class: "plain-button ghost" }, ["Archive"]);
    arch.addEventListener("click", () => post(`/api/sessions/${sid}/archive`, { archived: true }));
    row.push(rename, pin, arch);
  }
  return el("div", { class: "hdr-actions" }, row);
}

function render(runner, models, caps, projects, sessions) {
  _host.textContent = "";
  const caph = el("button", { class: "hdr-caps", "aria-label": "Capabilities — open the Hub" },
    [caps.summary || "capabilities"]);
  caph.addEventListener("click", () => { location.hash = "hub"; });
  _host.appendChild(el("div", { class: "convo-header" }, [
    el("div", { class: "hdr-left" }, [
      scopeSelect(runner, projects), titleCluster(runner), actions(runner, sessions),
    ]),
    el("div", { class: "hdr-right" },
      [modelSelect(models), effortSelect(models), modeSelect(runner), caph].filter(Boolean)),
  ]));
}
