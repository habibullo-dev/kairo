// Attended task creation and task-run history.  Team follow-ups are untrusted model planning
// notes: this module only ever opens an editable human form.  It never calls the scheduler until
// the person explicitly submits that form.
import { el } from "./dom.js";
import { showToast } from "./feedback.js";

let activeDialog = null;

function sameContext(left, right) {
  return Boolean(left && right && left.session_id === right.session_id && left.project_id === right.project_id);
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

function button(label, className) {
  return el("button", { type: "button", class: className, text: label });
}

function field(labelText, control, hint = null) {
  const label = el("label", { class: "task-draft-field" }, [
    el("span", { class: "task-draft-label", text: labelText }), control,
  ]);
  if (hint) label.append(el("span", { class: "task-draft-hint", text: hint }));
  return label;
}

function sourcePayload(source) {
  const run = Number.isInteger(Number(source.runId)) ? `#${Number(source.runId)}` : "unknown";
  const runTitle = String(source.runTitle || "Team run").trim() || "Team run";
  const goal = String(source.goal || "").trim();
  return [
    `Source run: ${run} — ${runTitle}`,
    "Model follow-up (untrusted planning note; reviewed by a human before scheduling):",
    goal || "(No goal was recorded.)",
    "",
    "Human-reviewed task instructions:",
    goal,
  ].join("\n");
}

function scheduleControl(kind, schedule) {
  if (kind.value === "once") {
    schedule.replaceChildren(field("Run once at", el("input", {
      class: "dialog-input", type: "datetime-local", required: true,
      "aria-label": "Run once at",
    }), "Choose a future local time. Nothing runs when this form opens."));
    return;
  }
  if (kind.value === "cron") {
    schedule.replaceChildren(field("Cron expression", el("input", {
      class: "dialog-input", type: "text", required: true, placeholder: "0 9 * * 1-5",
      "aria-label": "Cron expression",
    }), "Five fields: minute hour day-of-month month day-of-week. Uses this machine's timezone."));
    return;
  }
  schedule.replaceChildren(field("Repeat every (seconds)", el("input", {
    class: "dialog-input", type: "number", required: true, min: "60", step: "1",
    placeholder: "3600", "aria-label": "Repeat every seconds",
  }), "Minimum 60 seconds. A job can start an unattended model turn each time it fires."));
}

// Opens an editable draft and returns true only after the person submits a valid task to the
// existing human-authority route.  source must be provenance only; it never selects a schedule.
export function openTaskDraft(source, api) {
  // The task is reviewed in this exact chat/project context. This is only an optimistic UI
  // freshness guard; the server compares it to the live workspace under its transition lock.
  const expectedContext = api.state.context && {
    session_id: api.state.context.session_id,
    project_id: api.state.context.project_id,
  };
  return new Promise((resolve) => {
    if (activeDialog) {
      // Never replace a dialog while its human-approved write is in flight. A late response from
      // the old draft must not close or resolve the new one.
      if (activeDialog.saving) { resolve(false); return; }
      close(false);
    }
    const overlay = el("div", { class: "dialog-overlay", role: "presentation" });
    const card = el("section", {
      class: "dialog-card task-draft-dialog", role: "dialog", "aria-modal": "true",
      "aria-label": "Review and schedule task",
    });
    const title = el("input", {
      class: "dialog-input", type: "text", required: true, maxlength: "240",
      "aria-label": "Task title",
    });
    title.value = String(source.title || "Follow up").trim() || "Follow up";
    const payload = el("textarea", {
      class: "dialog-input task-draft-payload", rows: "8", required: true,
      "aria-label": "Task instructions and provenance",
    });
    payload.value = sourcePayload(source);
    const kind = el("select", { class: "dialog-input", "aria-label": "Task kind" }, [
      el("option", { value: "reminder", text: "Reminder (notify me)" }),
      el("option", { value: "job", text: "Job (starts an unattended task run)" }),
    ]);
    const scheduleKind = el("select", { class: "dialog-input", "aria-label": "Schedule type" }, [
      el("option", { value: "once", text: "Run once" }),
      el("option", { value: "cron", text: "Recurring calendar schedule" }),
      el("option", { value: "interval", text: "Recurring interval" }),
    ]);
    const schedule = el("div", { class: "task-draft-schedule" });
    scheduleControl(scheduleKind, schedule);
    scheduleKind.addEventListener("change", () => scheduleControl(scheduleKind, schedule));

    const error = el("div", { class: "task-draft-error", role: "alert", hidden: true });
    const cancel = button("Cancel", "dialog-button secondary");
    const submit = button("Schedule task", "dialog-button primary");
    submit.type = "submit";
    const form = el("form", { class: "task-draft-form" }, [
      el("h2", { class: "dialog-title", text: "Review and schedule task" }),
      el("p", { class: "dialog-message", text: "This was suggested by a team run. Review and edit it before scheduling; opening this draft never runs work." }),
      field("Title", title),
      field("Instructions and source provenance", payload, "The source run is included for audit context. Edit these instructions as needed."),
      el("div", { class: "task-draft-grid" }, [
        field("Task kind", kind), field("Schedule", scheduleKind),
      ]),
      schedule, error,
      el("div", { class: "dialog-actions" }, [cancel, submit]),
    ]);
    card.append(form);
    overlay.append(card);

    const showError = (message) => {
      error.hidden = false;
      error.textContent = message || "Task could not be scheduled. Review the fields and try again.";
    };
    let owner = null;
    cancel.addEventListener("click", () => close(false, owner));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (owner?.saving) return;
      const spec = schedule.querySelector("input")?.value.trim() || "";
      if (!title.value.trim() || !payload.value.trim() || !spec) {
        showError("Title, instructions, and a schedule are required.");
        return;
      }
      if (!sameContext(api.state.context, expectedContext)) {
        showError("This chat or project changed. Review the task again before scheduling it.");
        return;
      }
      owner.saving = true;
      submit.disabled = true;
      submit.textContent = "Scheduling…";
      cancel.disabled = true;
      title.disabled = true;
      payload.disabled = true;
      kind.disabled = true;
      scheduleKind.disabled = true;
      schedule.querySelector("input")?.setAttribute("disabled", "");
      error.hidden = true;
      try {
        const result = await api.post("/api/tasks/create", {
          kind: kind.value,
          title: title.value.trim(),
          payload: payload.value.trim(),
          schedule_kind: scheduleKind.value,
          schedule_spec: spec,
          expected_context: expectedContext,
        });
        if (result.ok && result.data?.ok) {
          owner.saving = false;
          close(true, owner);
          showToast("Task scheduled. It has not run yet.");
          return;
        }
        if (activeDialog !== owner) return;
        owner.saving = false;
        submit.disabled = false;
        submit.textContent = "Schedule task";
        cancel.disabled = false;
        title.disabled = false;
        payload.disabled = false;
        kind.disabled = false;
        scheduleKind.disabled = false;
        schedule.querySelector("input")?.removeAttribute("disabled");
        showError(result.data?.message);
      } catch {
        if (activeDialog !== owner) return;
        owner.saving = false;
        submit.disabled = false;
        submit.textContent = "Schedule task";
        cancel.disabled = false;
        title.disabled = false;
        payload.disabled = false;
        kind.disabled = false;
        scheduleKind.disabled = false;
        schedule.querySelector("input")?.removeAttribute("disabled");
        showError("Task could not be scheduled. Check your connection and try again.");
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
    title.focus();
  });
}

function runText(run) {
  if (run.error) return `Error: ${run.error}`;
  if (run.result_text) return run.result_text;
  if (run.status === "running") return "This run is still in progress.";
  if (run.status === "missed") return "This scheduled occurrence was missed; it was not executed.";
  return "No result was recorded.";
}

// Run history is read-only and fetched only after the person asks to inspect a specific task.
export async function openTaskHistory(task, api) {
  const rows = await api.get(`/api/tasks/${encodeURIComponent(task.id)}/runs`);
  if (rows === null) {
    showToast("Task history is unavailable right now.", "error");
    return;
  }
  if (activeDialog) close(false);
  await new Promise((resolve) => {
    const overlay = el("div", { class: "dialog-overlay", role: "presentation" });
    const card = el("section", {
      class: "dialog-card task-history-dialog", role: "dialog", "aria-modal": "true",
      "aria-label": `Run history for ${task.title || "task"}`,
    });
    const closeButton = button("Close", "dialog-button secondary");
    const body = el("div", { class: "task-history-list" });
    if (!rows.length) {
      body.append(el("p", { class: "dialog-message", text: "This task has not run yet." }));
    } else {
      for (const run of rows) {
        const dates = [
          `scheduled ${run.scheduled_for || "—"}`,
          run.started_at ? `started ${run.started_at}` : null,
          run.finished_at ? `finished ${run.finished_at}` : null,
        ].filter(Boolean).join(" · ");
        body.append(el("article", { class: "task-history-run" }, [
          el("div", { class: "task-history-run-head" }, [
            el("strong", { text: `Run #${run.id} · ${run.status || "unknown"}` }),
            run.cost_usd == null ? null : el("span", { class: "dim", text: `$${Number(run.cost_usd).toFixed(4)}` }),
          ]),
          el("div", { class: "dim mono", text: dates }),
          el("pre", { class: "task-history-result", text: runText(run) }),
        ]));
      }
    }
    card.append(
      el("h2", { class: "dialog-title", text: `Run history · ${task.title || "Task"}` }),
      el("p", { class: "dialog-message", text: "Recorded task executions and outcomes. Viewing history does not re-run the task." }),
      el("div", { class: "task-history-provenance" }, [
        el("strong", { text: "Task instructions and source provenance" }),
        el("pre", {
          class: "task-history-result task-history-payload",
          text: task.payload || "No instructions were recorded.",
        }),
      ]),
      body,
      el("div", { class: "dialog-actions" }, [closeButton]),
    );
    overlay.append(card);
    closeButton.addEventListener("click", () => close(false));
    overlay.addEventListener("click", (event) => { if (event.target === overlay) close(false); });
    const restoreFocus = document.activeElement;
    const onKeydown = (event) => { if (event.key === "Escape") close(false); };
    document.addEventListener("keydown", onKeydown);
    activeDialog = { overlay, resolve, onKeydown, restoreFocus };
    document.body.append(overlay);
    closeButton.focus();
  });
}
