// Project-assessment report dialog. The server has already reduced the durable model output to
// a bounded, exact-project view; this module keeps the browser seam safe by rendering every
// model-controlled string through textContent. It is view-only and starts no remediation.
import { el } from "./dom.js";
import { showToast } from "./feedback.js";
import { pushEscape } from "./keys.js";

let activeClose = null;

function text(value, fallback = "") {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function countChip(label, value) {
  const count = Number.isInteger(value) && value >= 0 ? value : 0;
  return el("span", { class: "chip", text: `${count} ${label}` });
}

function findingCard(finding) {
  const card = el("article", { class: "project-report-finding" });
  const heading = el("strong", { text: text(finding?.title, "Untitled finding") });
  const meta = [finding?.severity, finding?.confidence ? `${finding.confidence} confidence` : ""]
    .filter((value) => typeof value === "string" && value).join(" · ");
  card.append(heading);
  if (meta) card.append(el("div", { class: "dim", text: meta }));
  card.append(el("p", { text: text(finding?.detail, "No detail supplied.") }));
  const evidence = Array.isArray(finding?.evidence) ? finding.evidence : [];
  if (evidence.length) {
    card.append(el("div", {
      class: "project-report-evidence",
      text: `Evidence: ${evidence.map((item) => text(item?.ref)).filter(Boolean).join(", ")}`,
    }));
  }
  return card;
}

function findingsSection(title, note, findings) {
  const section = el("section", { class: "project-report-section" });
  section.append(el("h3", { text: title }));
  if (note) section.append(el("p", { class: "project-report-note", text: note }));
  const rows = Array.isArray(findings) ? findings : [];
  if (!rows.length) {
    section.append(el("div", { class: "dim", text: "No supported findings in this category." }));
  } else {
    for (const finding of rows) section.append(findingCard(finding));
  }
  return section;
}

function recommendationsSection(report) {
  const section = el("section", { class: "project-report-section" });
  section.append(el("h3", { text: "Recommendations" }));
  const rows = Array.isArray(report?.recommendations) ? report.recommendations : [];
  if (!rows.length) {
    section.append(el("div", { class: "dim", text: "No supported recommendations." }));
    return section;
  }
  for (const recommendation of rows) {
    const card = el("article", { class: "project-report-finding" });
    card.append(
      el("strong", { text: text(recommendation?.title, "Untitled recommendation") }),
      el("div", { class: "dim", text: `${text(recommendation?.priority, "medium")} priority` }),
      el("p", { text: text(recommendation?.goal, "No goal supplied.") }),
    );
    const reportId = report?.id;
    const recommendationIndex = recommendation?.index;
    if (
      report?.status === "current"
      && recommendation?.studio_available === true
      && Number.isSafeInteger(reportId)
      && reportId > 0
      && Number.isSafeInteger(recommendationIndex)
      && recommendationIndex >= 0
      && recommendationIndex <= 4
    ) {
      const review = el("button", {
        class: "chip-btn project-report-review", type: "button", text: "Review with AI team",
      });
      review.addEventListener("click", () => {
        if (activeClose) activeClose();
        location.hash = `studio/report/${reportId}/${recommendationIndex}`;
      });
      card.append(
        el("div", {
          class: "dim",
          text: "Opens Studio to review scope and cost. Nothing starts automatically.",
        }),
        review,
      );
    }
    section.append(card);
  }
  return section;
}

function showReport(report) {
  if (activeClose) activeClose();
  const overlay = el("div", { class: "dialog-overlay", role: "presentation" });
  const card = el("section", {
    class: "dialog-card project-report-dialog",
    role: "dialog",
    "aria-modal": "true",
    "aria-label": "Project assessment report",
  });
  const closeButton = el("button", {
    class: "dialog-button secondary", type: "button", text: "Close",
  });
  const restoreFocus = document.activeElement;
  let unregisterEscape = () => {};
  const close = () => {
    unregisterEscape();
    overlay.remove();
    if (activeClose === close) activeClose = null;
    if (restoreFocus instanceof HTMLElement && restoreFocus.isConnected) restoreFocus.focus();
  };
  activeClose = close;
  unregisterEscape = pushEscape(close);
  closeButton.addEventListener("click", close);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });

  const counts = report?.counts && typeof report.counts === "object" ? report.counts : {};
  const countRow = el("div", { class: "chip-row" }, [
    countChip("strengths", counts.strengths),
    countChip("weaknesses", counts.weaknesses),
    countChip("security candidates", counts.security_candidates),
    countChip("frontend/backend gaps", counts.frontend_backend_gaps),
    countChip("test gaps", counts.test_reliability_gaps),
  ]);
  card.append(
    el("div", { class: "project-report-head" }, [
      el("div", {}, [
        el("div", { class: "chat-kicker", text: "Read-only project assessment" }),
        el("h2", { class: "dialog-title", text: "What Kairo found" }),
      ]),
      el("span", { class: "chip", text: text(report?.status, "unknown") }),
    ]),
    el("p", {
      class: "project-report-disclaimer",
      text: "Model-generated analysis. Treat it as a prioritized review queue, not verified fact.",
    }),
    el("p", { class: "project-report-summary", text: text(report?.summary, "No summary supplied.") }),
    countRow,
    findingsSection("Strengths", "Supported positive signals in the imported snapshot.", report?.strengths),
    findingsSection("Weaknesses", "Areas that may limit quality, maintainability, or delivery.", report?.weaknesses),
    findingsSection(
      "Candidate security findings · not independently validated",
      "These are unvalidated candidates, not confirmed vulnerabilities. Validate before remediation or disclosure.",
      report?.security_candidates,
    ),
    findingsSection("Frontend/backend gaps", "Backend capabilities that may be missing, broken, or unclear in the user experience.", report?.frontend_backend_gaps),
    findingsSection("Test and reliability gaps", "Coverage, failure-mode, and operational concerns supported by report evidence.", report?.test_reliability_gaps),
    recommendationsSection(report),
    el("div", { class: "dialog-actions" }, [closeButton]),
  );
  overlay.append(card);
  document.body.append(overlay);
  closeButton.focus();
}

export async function openProjectReport(api, reportId) {
  const id = Number(reportId);
  if (!Number.isInteger(id) || id < 1) {
    showToast("This project assessment link is invalid.", "error");
    return;
  }
  const data = await api.get(`/api/project-intelligence/reports/${encodeURIComponent(id)}`);
  if (!data?.report) {
    showToast("This project assessment is unavailable in the current project.", "error");
    return;
  }
  showReport(data.report);
}
