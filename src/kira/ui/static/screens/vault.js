// Vault — KB stats + the unreviewed review queue (kb review) + lint. Amber flags anything
// awaiting review (quarantined untrusted content).
import { esc, escAttr } from "../ui/dom.js";
import {
  beginVaultReview, bindVaultReview, pendingVaultReview, settleVaultReview,
} from "../ui/vault-review.js";

function setReviewBusy(card, busy, action = null) {
  for (const control of card.querySelectorAll(".review-actions button")) control.disabled = busy;
  card.setAttribute("aria-busy", busy ? "true" : "false");
  if (busy) {
    const feedback = card.querySelector(".vault-review-feedback");
    if (feedback) feedback.textContent = action === "approve" ? "Approving…" : "Rejecting…";
  }
}

export async function render(container, api) {
  const data = await api.getRequired("/api/vault");
  if (!data) { container.innerHTML = unavailable("Vault", "knowledge base off"); return; }
  const stats = data.stats || {};
  const readiness = data.project_readiness;
  const cells = Object.entries(stats)
    .map(([k, v]) => `<div class="stat"><span class="n">${esc(String(v))}</span><span class="l">${esc(k)}</span></div>`)
    .join("");
  container.innerHTML = `
    <div class="rise"><h1>Vault</h1>
      <div class="sub">Project knowledge is retrieved by relevance, then expanded through verified local relationships.</div></div>
    <div class="card rise"><div class="stat-row">${cells || '<span class="dim">empty</span>'}</div></div>
    ${readiness ? `<div class="card rise vault-readiness">
      <div class="card-head"><div class="t">Project knowledge readiness</div><span class="tag ${readiness.ready ? "ok" : "amber"}">${readiness.ready ? "ready" : "needs files"}</span></div>
      <div class="stat-row"><div class="stat"><span class="n">${esc(String(readiness.sources || 0))}</span><span class="l">project files</span></div>
        <div class="stat"><span class="n">${esc(String(readiness.indexed_chunks || 0))}</span><span class="l">indexed sections</span></div>
        <div class="stat"><span class="n">${esc(String(readiness.import_links || 0))}</span><span class="l">verified imports</span></div></div>
      <div class="dim vault-readiness-detail">${esc(readiness.detail || "")}</div>
      <div class="action-row vault-readiness-actions"><a class="plain-button ghost" href="#workspace/${escAttr(String(readiness.project_id))}/graph">Open project graph</a><a class="plain-button ghost" href="#chat">Ask about this project</a></div>
      <div class="dim vault-readiness-detail">Kira retrieves relevant sections and direct verified dependencies for a question; it does not put the entire project into every prompt.</div>
    </div>` : ""}
    <div class="card rise"><div class="card-head"><div class="t">Add to the vault</div></div>
      <div class="ingest-box">
        <input id="vault-ingest-input" placeholder="file path, folder, or https:// URL" autocomplete="off">
        <button class="rowbtn" id="vault-ingest-go">Ingest</button>
      </div>
      <div class="dim vault-ingest-hint">Files/URLs land reviewed; a folder bulk-ingests (secrets skipped, symlinks refused). Use <span class="mono">kb ingest</span> in the terminal for folders.</div>
      <div id="vault-ingest-out" class="dim mono vault-ingest-output"></div></div>
    <div class="card rise"><div class="card-label amber">Review queue · unreviewed</div><div id="vault-queue"></div></div>
    <div class="card rise"><div class="card-head"><div class="t">Lint</div>
      <button class="rowbtn" id="vault-lint">Run lint</button></div>
      <div id="vault-lint-out" class="dim mono">—</div></div>`;

  const ingestInput = container.querySelector("#vault-ingest-input");
  const ingest = async () => {
    const target = ingestInput.value.trim();
    if (!target) return;
    const body = /^https?:\/\//i.test(target) ? { url: target } : { path: target };
    const res = await api.post("/api/vault/ingest", body);
    const out = container.querySelector("#vault-ingest-out");
    out.textContent = res.ok ? `${res.data.action}: source #${res.data.source_id}` : `— ${res.data.message} —`;
    if (res.ok) await api.refreshRoute();
  };
  container.querySelector("#vault-ingest-go").addEventListener("click", ingest);
  ingestInput.addEventListener("keydown", (e) => { if (e.key === "Enter") ingest(); });

  const q = container.querySelector("#vault-queue");
  const items = data.unreviewed || [];
  const projectId = data.project_id ?? api.state?.context?.project_id ?? null;
  if (!items.length) {
    q.innerHTML = `<div class="dim">Nothing to review. You're clear.</div>`;
  } else {
    // One card per source WITH a content preview — approving is informed, one at a time
    // (no bulk-approve: promoting untrusted content into search is a deliberate, per-item act).
    q.innerHTML = "";
    for (const s of items) {
      const card = document.createElement("div");
      card.className = "review-item";
      const sourceKey = String(s.id);
      card.dataset.vaultSourceId = sourceKey;
      const head = document.createElement("div");
      head.className = "review-head";
      head.innerHTML = `<span>${esc(s.title || "(untitled)")} <span class="tag amber">${esc(s.review_status)}</span></span>
        <span class="mono dim">${esc(s.origin)}</span>`;
      const actions = document.createElement("div");
      actions.className = "review-actions";
      const feedback = document.createElement("div");
      feedback.className = "dim vault-review-feedback";
      feedback.setAttribute("role", "status");
      feedback.setAttribute("aria-live", "polite");
      const bindOperation = (operation) => bindVaultReview(operation, ({ pending, result }) => {
        if (pending) {
          setReviewBusy(card, true, operation.action);
          return true;
        }
        if (!card.isConnected) return false;
        if (result?.ok) {
          void api.refreshRoute();
          return true;
        }
        setReviewBusy(card, false);
        feedback.textContent = result?.data?.message
          || "This review action could not be completed.";
        return true;
      });
      const review = async (action) => {
        const operation = beginVaultReview(api, projectId, s.id, action);
        if (!operation) return;
        bindOperation(operation);
        let result;
        try {
          const path = action === "approve"
            ? `/api/vault/sources/${s.id}/approve`
            : `/api/vault/sources/${s.id}/reject`;
          result = await api.post(path);
        } catch {
          result = {
            ok: false,
            data: {
              message: "Kira couldn't confirm whether the review decision was saved. Refresh the review queue before trying again.",
            },
          };
        }
        await settleVaultReview(operation, result);
      };
      const approve = btn("Approve", () => review("approve"));
      const reject = btn("Reject", () => review("reject"));
      actions.append(approve, reject);
      head.appendChild(actions);
      const preview = document.createElement("pre");
      preview.className = "block review-preview";
      preview.textContent = s.preview || "(no preview available)";  // textContent — untrusted content is never HTML
      card.append(head, preview, feedback);
      const operation = pendingVaultReview(api, projectId, s.id);
      if (operation) bindOperation(operation);
      q.appendChild(card);
    }
  }
  container.querySelector("#vault-lint").addEventListener("click", async () => {
    const r = await api.get("/api/vault/lint");
    container.querySelector("#vault-lint-out").textContent = r ? JSON.stringify(r) : "—";
  });
}

function btn(label, fn) {
  const b = document.createElement("button");
  b.className = "rowbtn";
  b.textContent = label;
  b.addEventListener("click", fn);
  return b;
}
function unavailable(name, why) {
  return `<div class="rise"><h1>${name}</h1><div class="sub">Unavailable — ${why}.</div></div>`;
}
