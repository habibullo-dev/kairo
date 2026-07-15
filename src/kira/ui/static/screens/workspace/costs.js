// Project Workspace — Costs tab (Phase 11 T10). Read-only spend by period + a budget warning +
// an optional by-provider breakdown. All values come from readmodels.costs_overview; no writes.
import { el } from "../../ui/dom.js";
import { emptyState, chip, row, section } from "./_util.js";
import { money } from "../../ui/format.js";

const PERIODS = [["today", "Today"], ["week", "This week"], ["month", "This month"]];

export async function render(container, api, ctx) {
  container.textContent = "";
  const data = await api.get("/api/costs?project_id=" + encodeURIComponent(ctx.projectId));
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }

  const tiles = PERIODS.filter(([k]) => data[k]).map(([k, label]) =>
    el("div", { class: "metric" }, [
      el("div", { class: "n" }, [money(data[k].cost_usd)]),
      el("div", { class: "l" }, [label]),
    ]),
  );
  if (!tiles.length) {
    container.appendChild(emptyState("No spend yet", "Costs land here once this project runs a model or service."));
    return;
  }

  // A non-ok monthly budget warning sits above the metrics (attention, not alarm).
  const warn = data.budget_warning;
  if (warn && warn.level && warn.level !== "ok") {
    const scope = money(warn.month_spend_usd) + " of " + money(warn.cap_usd) + " monthly cap";
    const msg = warn.level === "hard"
      ? "Monthly budget reached — " + scope + " spent this month."
      : "Approaching the monthly budget — " + scope + " spent this month.";
    container.appendChild(el("div", { class: "risk-banner" }, [msg]));
  }

  container.appendChild(el("div", { class: "cost-row" }, tiles));

  // Optional: where this month's spend went, by provider. Unpriced calls are flagged, never $0.
  const provs = (data.by_provider || []).filter((r) => r && r.provider != null);
  if (provs.length) {
    const rows = provs.slice(0, 6).map((r) =>
      row("◈", r.provider, (r.calls || 0) + " calls" + (r.unpriced ? " · " + r.unpriced + " unpriced" : ""),
        { trailing: chip(money(r.cost_usd)) }),
    );
    container.appendChild(section("This month by provider", rows));
  }
}
