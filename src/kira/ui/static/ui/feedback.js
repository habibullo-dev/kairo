// Small, in-app feedback primitives for attended workstation actions.  They deliberately use
// DOM construction/textContent rather than browser confirm/prompt dialogs or HTML interpolation.
// These are presentation only: callers still invoke the same pre-existing, server-authorized
// routes after a person makes a choice.
import { el } from "./dom.js";

let activeDialog = null;

function closeDialog(value) {
  const current = activeDialog;
  if (!current) return;
  activeDialog = null;
  document.removeEventListener("keydown", current.onKeydown);
  current.overlay.remove();
  current.restoreFocus?.focus?.();
  current.resolve(value);
}

function dialogFrame({ title, message, confirmLabel, cancelLabel, tone, inputValue = null }) {
  return new Promise((resolve) => {
    if (activeDialog) closeDialog(null);
    const overlay = el("div", { class: "dialog-overlay", role: "presentation" });
    const card = el("section", {
      class: `dialog-card ${tone || "default"}`,
      role: "dialog", "aria-modal": "true", "aria-label": title,
    });
    const heading = el("h2", { class: "dialog-title", text: title });
    const body = el("p", { class: "dialog-message", text: message });
    let input = null;
    if (inputValue !== null) {
      input = el("input", {
        class: "dialog-input", type: "text", value: inputValue,
        "aria-label": "Chat title", maxlength: "120",
      });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && input.value.trim()) closeDialog(input.value.trim());
      });
    }
    const cancel = el("button", {
      class: "dialog-button secondary", type: "button", text: cancelLabel,
    });
    const confirm = el("button", {
      class: `dialog-button primary ${tone || "default"}`, type: "button", text: confirmLabel,
    });
    cancel.addEventListener("click", () => closeDialog(null));
    confirm.addEventListener("click", () => closeDialog(input ? input.value.trim() || null : true));
    const actions = el("div", { class: "dialog-actions" }, [cancel, confirm]);
    card.append(heading, body);
    if (input) card.append(input);
    card.append(actions);
    overlay.append(card);
    overlay.addEventListener("click", (event) => { if (event.target === overlay) closeDialog(null); });
    const restoreFocus = document.activeElement;
    const onKeydown = (event) => {
      if (event.key === "Escape") { closeDialog(null); return; }
      if (event.key !== "Tab") return;
      const controls = [...card.querySelectorAll("button, input, [tabindex]:not([tabindex='-1'])")];
      const first = controls[0];
      const last = controls.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeydown);
    activeDialog = { overlay, resolve, onKeydown, restoreFocus };
    document.body.append(overlay);
    (input || cancel).focus();
  });
}

export function confirmDialog({ title, message, confirmLabel = "Continue", tone = "default" }) {
  return dialogFrame({ title, message, confirmLabel, cancelLabel: "Cancel", tone });
}

export function promptDialog({ title, message, value = "", confirmLabel = "Save" }) {
  return dialogFrame({ title, message, confirmLabel, cancelLabel: "Cancel", inputValue: value });
}

export function dismissFeedbackDialogs() { closeDialog(null); }

export function showToast(message, tone = "default") {
  let host = document.getElementById("kira-toasts");
  if (!host) {
    host = el("div", { id: "kira-toasts", class: "toast-stack", "aria-live": "polite" });
    document.body.append(host);
  }
  const toast = el("div", { class: `toast ${tone}`, role: "status", text: message });
  host.append(toast);
  setTimeout(() => toast.remove(), 4200);
}
