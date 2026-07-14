// Attended task creation and task-run history.  Team follow-ups are untrusted model planning
// notes: this module only ever opens an editable human form.  It never calls the scheduler until
// the person explicitly submits that form.
import { el } from "./dom.js";
import { showToast } from "./feedback.js";

let activeDialog = null;
let dialogRequestRevision = 0;

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

export function dismissTaskDialogs() {
  dialogRequestRevision += 1;
  const current = activeDialog;
  if (!current) return false;
  current.saving = false;
  return close(false, current);
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

// Review one durable unattended ASK.  This is intentionally a sibling to task creation rather
// than a generic Gate approval: a parked run can approve ONLY this stored call once or reject it;
// it cannot create an "always" policy.  The server mints the nonce only after this exact payload
// is on a live local screen, then re-reads the task/run before its host callback may resume it.
export function openParkedTaskApproval(approval, api, handlers = {}) {
  dialogRequestRevision += 1;
  const runId = Number(approval && approval.run_id);
  if (!Number.isInteger(runId) || runId < 1) throw new Error("invalid parked task run");
  if (activeDialog && !close(false)) return null;

  const overlay = el("div", { class: "dialog-overlay", role: "presentation" });
  const card = el("section", {
    class: "dialog-card task-history-dialog", role: "dialog", "aria-modal": "true",
    "aria-label": "Review parked task approval",
  });
  const title = String(approval.task_title || "Task").trim() || "Task";
  const reason = String(approval.reason || "This action needs your approval.");
  const exact = {
    tool_id: String(approval.tool_id || ""),
    tool_name: String(approval.tool_name || ""),
    tool_input: approval.tool_input && typeof approval.tool_input === "object" ? approval.tool_input : {},
    tool_input_hash: String(approval.tool_input_hash || ""),
  };
  const status = el("div", { class: "task-draft-error", role: "status", text: "Preparing secure confirmation…" });
  const reject = button("Reject task run", "dialog-button secondary");
  const approve = button("Approve once & resume", "dialog-button primary");
  reject.disabled = true;
  approve.disabled = true;
  let nonce = null;
  let owner = null;

  const setWaiting = (message) => {
    status.className = "task-draft-error";
    status.textContent = message;
    reject.disabled = true;
    approve.disabled = true;
  };
  const setReady = () => {
    status.className = "task-draft-error";
    status.textContent = "Review the exact saved call, then choose once or reject it.";
    reject.disabled = false;
    approve.disabled = false;
  };
  const dismiss = () => {
    if (close(false, owner)) handlers.onDismissed?.();
  };
  const forceDismiss = () => {
    if (activeDialog !== owner) return;
    owner.saving = false;
    dismiss();
  };
  const resolve = async (action) => {
    if (!nonce || owner?.saving) return;
    owner.saving = true;
    reject.disabled = true;
    approve.disabled = true;
    setWaiting(action === "approve" ? "Submitting one-time approval…" : "Rejecting this parked run…");
    let result;
    try {
      result = await api.post(`/api/parked-task-approvals/${encodeURIComponent(runId)}/resolve`, {
        nonce, action,
      });
    } catch {
      result = { ok: false, data: {} };
    }
    if (activeDialog !== owner) return;
    if (result.ok) {
      owner.saving = false;
      handlers.onResolved?.();
      close(true, owner);
      return;
    }
    owner.saving = false;
    nonce = null; // a reserved/failed callback never revives the old one-time credential
    if (result.data && result.data.retry) {
      setWaiting("The task changed before it could be resolved. Reopening secure confirmation…");
      handlers.onRetry?.();
      return;
    }
    status.className = "task-draft-error show";
    status.textContent = String(result.data?.message || "This task approval could not be resolved.");
    // A transport/nonce/scope failure must not silently mint a new credential. The saved call
    // remains pending; close and reopen its history to prove a fresh visible review.
  };

  card.append(
    el("h2", { class: "dialog-title", text: "Review parked task approval" }),
    el("p", { class: "dialog-message", text: "This unattended task stopped before executing the action below. Approving resumes only this saved call once; it does not create an ongoing permission." }),
    el("div", { class: "task-history-provenance" }, [
      el("strong", { text: `Task #${Number(approval.task_id) || "?"} · ${title} · run #${runId}` }),
      el("div", { class: "dim", text: reason }),
      el("strong", { text: "Exact saved tool call" }),
      el("pre", { class: "task-history-result task-history-payload", text: JSON.stringify(exact, null, 2) }),
    ]),
    status,
    el("div", { class: "dialog-actions" }, [reject, approve]),
  );
  overlay.append(card);
  reject.addEventListener("click", () => { void resolve("reject"); });
  approve.addEventListener("click", () => { void resolve("approve"); });
  overlay.addEventListener("click", (event) => { if (event.target === overlay) dismiss(); });
  const restoreFocus = document.activeElement;
  const onKeydown = (event) => { if (event.key === "Escape") dismiss(); };
  document.addEventListener("keydown", onKeydown);
  owner = { overlay, onKeydown, restoreFocus, saving: false, resolve: () => {} };
  activeDialog = owner;
  document.body.append(overlay);
  approve.focus();
  handlers.onShown?.();

  return {
    runId,
    dismiss: forceDismiss,
    setNonce(value) {
      if (typeof value !== "string" || !value || activeDialog !== owner || owner.saving) return;
      nonce = value;
      setReady();
      approve.focus();
    },
  };
}

// Opens an editable draft and returns true only after the person submits a valid task to the
// existing human-authority route.  source must be provenance only; it never selects a schedule.
export function openTaskDraft(source, api) {
  dialogRequestRevision += 1;
  // The task is reviewed in this exact chat/project context. This is only an optimistic UI
  // freshness guard; the server compares it to the live workspace under its transition lock.
  const expectedContext = api.state.context && {
    session_id: api.state.context.session_id,
    project_id: api.state.context.project_id,
    context_revision: api.state.context.context_revision,
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
    const verification = el("textarea", {
      class: "dialog-input task-draft-payload", rows: "3",
      "aria-label": "Required phrases in the final job answer",
      placeholder: "STATUS: complete\nFILES-CHANGED",
    });
    const verificationField = field(
      "Expected final-answer phrases (optional)", verification,
      "One literal phrase per line. This checks only the final answer text; it does not prove an external action occurred."
    );
    const syncVerification = () => {
      const isJob = kind.value === "job";
      verificationField.hidden = !isJob;
      verification.disabled = !isJob;
    };
    syncVerification();
    kind.addEventListener("change", syncVerification);
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
      schedule, verificationField, error,
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
      const verifyContains = kind.value === "job"
        ? verification.value.split("\n").map((value) => value.trim()).filter(Boolean)
        : [];
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
      verification.disabled = true;
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
          verify_contains: verifyContains.length ? verifyContains : null,
          expected_context: expectedContext,
        });
        if (activeDialog !== owner) return;
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
        syncVerification();
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
        syncVerification();
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
  if (run.approval_state === "pending") {
    return "This run is parked before a tool call. Review its exact saved approval before it can continue.";
  }
  if (run.error) {
    return run.result_text
      ? `Error: ${run.error}\n\nFinal output:\n${run.result_text}`
      : `Error: ${run.error}`;
  }
  if (run.result_text) return run.result_text;
  if (run.status === "running") return "This run is still in progress.";
  if (run.status === "missed") return "This scheduled occurrence was missed; it was not executed.";
  return "No result was recorded.";
}

// Run history is read-only and fetched only after the person asks to inspect a specific task.
export async function openTaskHistory(task, api) {
  const historyRevision = ++dialogRequestRevision;
  const authorityToken = typeof api.authorityToken === "function" ? api.authorityToken() : null;
  const requestIsCurrent = () => (
    (authorityToken === null || typeof api.authorityIsCurrent !== "function"
      || api.authorityIsCurrent(authorityToken))
    && (typeof api.renderIsCurrent !== "function" || api.renderIsCurrent())
  );
  const rows = await api.get(`/api/tasks/${encodeURIComponent(task.id)}/runs`);
  if (historyRevision !== dialogRequestRevision || !requestIsCurrent()) return;
  if (rows === null) {
    showToast("Task history is unavailable right now.", "error");
    return;
  }
  if (activeDialog && !close(false)) return;
  if (historyRevision !== dialogRequestRevision) return;
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
        const entry = el("article", { class: "task-history-run" }, [
          el("div", { class: "task-history-run-head" }, [
            el("strong", { text: `Run #${run.id} · ${run.status || "unknown"}` }),
            run.verification_status && run.verification_status !== "not_configured"
              ? el("span", { class: "dim", text: `verification: ${run.verification_status}` })
              : null,
            run.cost_usd == null ? null : el("span", { class: "dim", text: `$${Number(run.cost_usd).toFixed(4)}` }),
          ]),
          el("div", { class: "dim mono", text: dates }),
          run.verification_summary
            ? el("div", { class: "dim", text: run.verification_summary })
            : null,
          el("pre", { class: "task-history-result", text: runText(run) }),
        ]);
        if (run.approval_state === "pending" && run.continuation) {
          const review = button("Review exact approval", "dialog-button primary");
          review.addEventListener("click", () => {
            const continuation = run.continuation || {};
            const shown = api.reviewParkedTask({
              run_id: run.id,
              task_id: task.id,
              task_title: task.title,
              project_id: task.project_id,
              tool_id: continuation.tool_id,
              tool_name: continuation.tool_name,
              tool_input: continuation.tool_input,
              tool_input_hash: continuation.tool_input_hash,
              reason: continuation.decision_reason,
            });
            if (!shown) showToast("This parked task is outside the current workspace.", "error");
          });
          entry.append(el("div", { class: "dialog-actions" }, [review]));
        }
        body.append(entry);
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
      task.verification?.terms?.length
        ? el("div", { class: "task-history-provenance" }, [
          el("strong", { text: "Expected final-answer phrases" }),
          el("pre", {
            class: "task-history-result task-history-payload",
            text: task.verification.terms.join("\n"),
          }),
          el("p", {
            class: "dialog-message",
            text: "This is a literal final-answer check, not proof that an external action occurred.",
          }),
        ])
        : null,
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
