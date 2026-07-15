// Workspace › Activity tab (Phase 11 T10). A read-only, metadata-only, time-ordered feed of
// what happened in this project — artifacts filed, orchestration runs, and chats.
import { emptyState, row, section } from "./_util.js";
import { relTime } from "../../ui/format.js";

const ICON = { artifact: "📄", run: "🧩", chat: "💬" };

export async function render(container, api, ctx) {
  container.textContent = "";
  const data = await api.get("/api/workspace/" + ctx.projectId + "/activity");
  if (!data) {
    container.appendChild(emptyState("Unavailable", "Couldn't load this tab — it'll refresh shortly."));
    return;
  }
  const events = data.events || [];
  if (!events.length) {
    container.appendChild(
      section("Activity", [emptyState("No activity yet", "Runs, artifacts, and chats in this project will show up here.")])
    );
    return;
  }
  const rows = events.map((e) => {
    const sub = e.type + (e.status ? " · " + e.status : "") + " · " + relTime(e.ts);
    return row(ICON[e.type] || "•", e.title, sub);
  });
  container.appendChild(section("Activity", rows));
}
