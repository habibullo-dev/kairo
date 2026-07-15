// Attended memory creation.  This dialog always starts with an empty field: the browser never
// promotes model output or a suggestion into durable memory on its own.  The existing endpoint
// owns project scope through the authenticated workspace handle, so this module deliberately
// sends no project_id.
import { el } from "./dom.js";
import { showToast } from "./feedback.js";

let activeDialog = null;

function sameContext(left, right) {
  return Boolean(
    left && right
    && left.session_id === right.session_id
    && left.project_id === right.project_id
    && left.context_revision === right.context_revision
  );
}

function close(value, owner = null) {
  const current = activeDialog;
  if (!current || (owner && current !== owner) || current.saving) return false;
  activeDialog = null;
  document.removeEventListener("keydown", current.onKeydown);
  current.overlay.remove();
  current.restoreFocus?.focus?.();
  current.resolve(value);
  return true;
}

export function dismissMemoryDraft() {
  const current = activeDialog;
  if (!current) return false;
  current.saving = false;
  return close(false, current);
}

function field(labelText, control, hint = null) {
  const label = el("label", { class: "memory-draft-field" }, [
    el("span", { class: "memory-draft-label", text: labelText }), control,
  ]);
  if (hint) label.append(el("span", { class: "memory-draft-hint", text: hint }));
  return label;
}

// Returns true only after a person has entered/reviewed content and explicitly saved it through
// the pre-existing human-authority endpoint. Opening or cancelling the form never writes memory.
export function openMemoryDraft(api) {
  // The content is reviewed in this exact chat/project context. This is only an optimistic UI
  // freshness guard; the server compares it to the live workspace under its transition lock.
  const expectedContext = api.state.context && {
    session_id: api.state.context.session_id,
    project_id: api.state.context.project_id,
    context_revision: api.state.context.context_revision,
  };
  return new Promise((resolve) => {
    if (activeDialog) {
      // Never replace a dialog while its user-approved write is in flight. A late response from
      // the old draft must not close or resolve the new one.
      if (activeDialog.saving) { resolve(false); return; }
      close(false);
    }
    const overlay = el("div", { class: "dialog-overlay", role: "presentation" });
    const card = el("section", {
      class: "dialog-card memory-draft-dialog", role: "dialog", "aria-modal": "true",
      "aria-label": "Remember something",
    });
    const content = el("textarea", {
      class: "dialog-input memory-draft-content", rows: "6", required: true, maxlength: "4000",
      "aria-label": "Memory to save", placeholder: "Write the fact or preference to remember…",
    });
    const type = el("select", { class: "dialog-input", "aria-label": "Memory type" }, [
      el("option", { value: "fact", text: "Fact" }),
      el("option", { value: "preference", text: "Preference" }),
      el("option", { value: "project", text: "Project context" }),
      el("option", { value: "episode", text: "Past event" }),
    ]);
    const error = el("div", { class: "memory-draft-error", role: "alert", hidden: true });
    const cancel = el("button", { class: "dialog-button secondary", type: "button", text: "Cancel" });
    const submit = el("button", { class: "dialog-button primary", type: "submit", text: "Save memory" });
    const form = el("form", { class: "memory-draft-form" }, [
      el("h2", { class: "dialog-title", text: "Remember something" }),
      el("p", {
        class: "dialog-message",
        text: "Review what you want Kira to retain. Nothing is saved until you press Save memory.",
      }),
      field("Memory", content, "Enter a durable fact, preference, project detail, or past event in your own words."),
      field("Type", type),
      error,
      el("div", { class: "dialog-actions" }, [cancel, submit]),
    ]);
    card.append(form);
    overlay.append(card);

    const showError = (message) => {
      error.hidden = false;
      error.textContent = message || "Memory could not be saved. Review it and try again.";
    };
    let owner = null;
    cancel.addEventListener("click", () => close(false, owner));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (owner?.saving) return;
      const reviewedContent = content.value.trim();
      if (!reviewedContent) {
        showError("Enter the memory you want to save.");
        content.focus();
        return;
      }
      if (!sameContext(api.state.context, expectedContext)) {
        showError("This chat or project changed. Review the memory again before saving it.");
        return;
      }
      owner.saving = true;
      submit.disabled = true;
      submit.textContent = "Saving…";
      cancel.disabled = true;
      content.disabled = true;
      type.disabled = true;
      error.hidden = true;
      try {
        const result = await api.post("/api/memory/remember", {
          content: reviewedContent,
          type: type.value,
          expected_context: expectedContext,
        });
        if (activeDialog !== owner) return;
        if (result.ok && result.data?.ok) {
          owner.saving = false;
          close(true, owner);
          showToast(result.data.action === "duplicate" ? "Memory already saved; its timestamp was refreshed." : "Memory saved.");
          return;
        }
        if (activeDialog !== owner) return;
        owner.saving = false;
        submit.disabled = false;
        submit.textContent = "Save memory";
        cancel.disabled = false;
        content.disabled = false;
        type.disabled = false;
        showError(result.data?.message);
      } catch {
        if (activeDialog !== owner) return;
        owner.saving = false;
        submit.disabled = false;
        submit.textContent = "Save memory";
        cancel.disabled = false;
        content.disabled = false;
        type.disabled = false;
        showError("Memory could not be saved. Check your connection and try again.");
      }
    });
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) close(false, owner);
    });
    const restoreFocus = document.activeElement;
    const onKeydown = (event) => {
      if (event.key === "Escape") { close(false, owner); return; }
      if (event.key !== "Tab") return;
      const controls = [...card.querySelectorAll("button, input, textarea, select, [tabindex]:not([tabindex='-1'])")];
      const first = controls[0];
      const last = controls.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeydown);
    owner = { overlay, resolve, onKeydown, restoreFocus, saving: false };
    activeDialog = owner;
    document.body.append(overlay);
    content.focus();
  });
}
