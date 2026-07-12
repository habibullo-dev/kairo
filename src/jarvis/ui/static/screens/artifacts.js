// Artifacts Library (Phase 11 T11) — the global screen at #artifacts (also reached from the
// Projects "Collections" row as #artifacts/{preset}). Left: a filterable list; right: a preview
// panel with a metadata block + a content preview. Untrusted content is rendered ONLY via
// textContent / a same-origin <img> from the hardened /content route (never raw markup, never an
// external link). Writes are the existing pin/label metadata routes — no new authority.
import { el } from "../ui/dom.js";
import { relTime } from "../ui/format.js";

const KIND_ICONS = {
  wiki_page: "📝", digest: "🗞", eval_report: "🧪", orchestration: "🧩",
  meeting_note: "🎙", report: "📄", draft: "✉", email_draft: "✉",
};

// Collection presets (from the Projects "Collections" row). Each is just a starting filter.
const PRESETS = {
  "recent-artifacts": {},
  "needs-review": {},
  "generated-week": { week: true },
  "by-team-model": {},
  "pinned-work": { pinned: true },
};

const _S = {
  container: null, api: null, all: [], selectedId: null,
  q: "", kind: null, pinnedOnly: false, week: false,
};

function icon(kind) {
  // own-property lookup only (a pathological kind like "__proto__" must not return an object).
  return Object.hasOwn(KIND_ICONS, kind) ? KIND_ICONS[kind] : "◆";
}

function runId(a) {
  const id = String(a && a.origin_id || "");
  return a && a.origin_type === "orchestration" && /^\d+$/.test(id) ? id : null;
}

function withinWeek(iso) {
  if (!iso) return false;
  const t = Date.parse(iso);
  return !Number.isNaN(t) && Date.now() - t < 7 * 86400 * 1000;
}

function filtered() {
  const ql = _S.q.toLowerCase();
  return _S.all.filter((a) => {
    if (_S.pinnedOnly && !a.pinned) return false;
    if (_S.kind && a.kind !== _S.kind) return false;
    if (_S.week && !withinWeek(a.created_at)) return false;
    if (ql) {
      const hay = `${a.title || ""} ${a.kind || ""} ${(a.labels || []).join(" ")} ${a.team || ""} ${a.model || ""}`.toLowerCase();
      if (!hay.includes(ql)) return false;
    }
    return true;
  });
}

async function refresh() {
  const data = await _S.api.get("/api/artifacts?limit=200");
  _S.all = (data && data.artifacts) || [];
  redraw();
}

function metaBlock(a) {
  const rows = [
    ["Kind", a.kind || "—"],
    ["Project", a.project_id == null ? "global" : `#${a.project_id}`],
    ["Team · role · model", [a.team, a.role, a.model].filter(Boolean).join(" · ") || "—"],
    ["Created", a.created_at ? relTime(a.created_at) : "—"],
    ["Sensitivity", a.sensitivity || "—"],
    ["Provenance", a.provenance_class || "—"],
    ["Content hash", a.content_hash ? a.content_hash.slice(0, 12) : "—"],
    ["Origin", `${a.origin_type}:${a.origin_id}`],
  ];
  if (a.external_uri) rows.push(["Source", a.external_uri]); // shown as TEXT — never a live link
  return el("div", { class: "art-meta" }, rows.map(([k, v]) =>
    el("div", { class: "art-meta-row" }, [
      el("span", { class: "art-meta-k" }, [k]),
      el("span", { class: "art-meta-v" }, [String(v)]),
    ])));
}

async function loadContent(a, bodyEl) {
  bodyEl.textContent = "";
  if (!a.has_content) {
    bodyEl.appendChild(el("div", { class: "dim" }, [
      a.external_uri ? "External reference — no inline preview." : "No content to preview.",
    ]));
    return;
  }
  const url = `/api/artifacts/${encodeURIComponent(a.id)}/content`;
  try {
    const r = await fetch(url, { headers: { accept: "*/*" } });
    if (!r.ok) {
      bodyEl.appendChild(el("div", { class: "dim" }, ["Preview unavailable."]));
      return;
    }
    const ct = r.headers.get("content-type") || "";
    if (ct.startsWith("image/")) {
      const img = el("img", { class: "art-img", alt: "artifact preview" });
      img.src = url; // same-origin, hardened route
      bodyEl.appendChild(img);
    } else {
      const text = await r.text();
      bodyEl.appendChild(el("pre", { class: "art-pre" }, [text])); // preformatted text content only
    }
  } catch {
    bodyEl.appendChild(el("div", { class: "dim" }, ["Preview unavailable."]));
  }
}

function previewPanel(a) {
  if (!a) {
    return el("div", { class: "empty-state" }, [
      el("h4", {}, ["No artifact selected"]),
      el("div", {}, ["Pick an artifact on the left to preview it and its provenance."]),
    ]);
  }
  const pin = el("button", { class: "plain-button" + (a.pinned ? "" : " ghost") }, [a.pinned ? "Unpin" : "Pin"]);
  pin.addEventListener("click", async () => {
    await _S.api.post(`/api/artifacts/${a.id}/pin`, { pinned: !a.pinned });
    await refresh();
  });
  const acts = [pin];
  if (a.has_content) {
    const open = el("button", { class: "plain-button ghost" }, ["Open ↗"]);
    open.addEventListener("click", () =>
      window.open(`/api/artifacts/${encodeURIComponent(a.id)}/content`, "_blank", "noopener"));
    acts.push(open);
  }
  const run = runId(a);
  if (run) {
    const openRun = el("button", { class: "plain-button ghost" }, ["Open run report"]);
    openRun.addEventListener("click", () => { location.hash = `studio/${run}`; });
    acts.push(openRun);
  }
  const labelInput = el("input", {
    class: "art-label-input", placeholder: "labels, comma-separated", value: (a.labels || []).join(", "),
  });
  const saveLabels = el("button", { class: "plain-button ghost" }, ["Save labels"]);
  saveLabels.addEventListener("click", async () => {
    const labels = labelInput.value.split(",").map((s) => s.trim()).filter(Boolean);
    await _S.api.post(`/api/artifacts/${a.id}/label`, { labels });
    await refresh();
  });

  const body = el("div", { class: "art-body" }, [el("div", { class: "dim" }, ["Loading…"])]);
  loadContent(a, body);

  return el("div", { class: "art-preview-inner" }, [
    el("div", { class: "panel-title" }, [el("h3", {}, [a.title || "(untitled)"]), el("span", { class: "p-chip" }, [a.kind || "?"])]),
    el("div", { class: "ws-rowacts" }, acts),
    metaBlock(a),
    el("div", { class: "art-label-edit" }, [labelInput, saveLabels]),
    body,
  ]);
}

// The list redraws on every filter/search change; the preview redraws ONLY when the selection
// changes (or after a pin/label write) — so a keystroke never re-fetches the selected artifact's
// content (no request storm, no image flicker).
function drawList() {
  const list = _S.container.querySelector("#art-list");
  if (!list) return;
  const items = filtered();
  list.textContent = "";
  if (!items.length) {
    list.appendChild(el("div", { class: "empty-state" }, [
      el("h4", {}, ["No artifacts"]),
      el("div", {}, [_S.all.length ? "None match these filters." : "Reports, drafts and outputs Kairo produces are filed here."]),
    ]));
    return;
  }
  for (const a of items) {
    const row = el("div", { class: "list-row art-row" + (a.id === _S.selectedId ? " sel" : "") }, [
      el("span", { class: "list-icon" }, [icon(a.kind)]),
      el("div", { class: "ws-rowmid" }, [
        el("div", { class: "lr-t" }, [a.title || "(untitled)"]),
        el("div", { class: "lr-s" }, [
          `${a.kind}${a.project_id == null ? "" : " · #" + a.project_id}${a.created_at ? " · " + relTime(a.created_at) : ""}`,
        ]),
      ]),
      el("span", { class: "p-chip" }, [a.pinned ? "★" : ""]),
    ]);
    row.addEventListener("click", () => {
      if (_S.selectedId === a.id) return;
      _S.selectedId = a.id;
      drawList();
      drawPreview();
    });
    list.appendChild(row);
  }
}

function drawPreview() {
  const preview = _S.container.querySelector("#art-preview");
  if (!preview) return;
  preview.textContent = "";
  preview.appendChild(previewPanel(_S.all.find((a) => a.id === _S.selectedId) || null));
}

function redraw() {
  drawList();
  drawPreview();
}

export async function render(container, api, args) {
  const rawKey = (args && args[0]) || "";
  const presetKey = rawKey.replace(/^view-/, "");
  const preset = PRESETS[presetKey] || null;
  _S.container = container;
  _S.api = api;
  _S.selectedId = null;
  _S.q = "";
  _S.kind = null;
  _S.pinnedOnly = !!(preset && preset.pinned);
  _S.week = !!(preset && preset.week);

  container.textContent = "";
  const data = await api.get("/api/artifacts?limit=200");
  _S.all = (data && data.artifacts) || [];

  const head = el("div", { class: "rise" }, [
    el("h1", {}, ["Artifacts"]),
    el("div", { class: "sub" }, [
      "Everything Kairo has produced — reports, drafts, pages, run outputs — with its provenance.",
    ]),
  ]);

  // filter bar: search + pinned toggle + kind chips (from the distinct kinds present)
  const search = el("input", {
    class: "ws-search", type: "search", placeholder: "Search artifacts…", "aria-label": "Search artifacts",
  });
  search.addEventListener("input", () => { _S.q = search.value; drawList(); });
  const pinToggle = el("button", { class: "filter-chip" + (_S.pinnedOnly ? " active" : "") }, ["Pinned"]);
  pinToggle.addEventListener("click", () => {
    _S.pinnedOnly = !_S.pinnedOnly;
    pinToggle.classList.toggle("active", _S.pinnedOnly);
    drawList();
  });
  const kinds = [...new Set(_S.all.map((a) => a.kind).filter(Boolean))].sort();
  const kindChips = el("div", { class: "art-kinds" }, [
    ...kinds.map((k) => {
      const c = el("button", { class: "filter-chip" }, [k]);
      c.addEventListener("click", () => {
        _S.kind = _S.kind === k ? null : k;
        for (const b of kindChips.querySelectorAll("button")) {
          b.classList.toggle("active", b.textContent === _S.kind);
        }
        drawList();
      });
      return c;
    }),
  ]);
  const filterBar = el("div", { class: "art-filters" }, [search, pinToggle, kindChips]);

  const layout = el("div", { class: "artifacts-layout" }, [
    el("div", { class: "art-list-col" }, [filterBar, el("div", { id: "art-list", class: "ws-list" }, [])]),
    el("div", { id: "art-preview", class: "surface art-preview" }, []),
  ]);

  container.append(head, layout);
  redraw();
}
