// Trace — the live event tree from the ring buffer (an advanced screen, not a default).
// Shows tool decisions (incl. denied), tool runs, sub-agent activity, model calls. Debug
// reveals raw payloads; the base view is one line per event.
export function render(container, api) {
  container.innerHTML = `
    <h1>Trace</h1><div class="sub">Every tool decision and call, including denied ones. Nothing hidden.</div>
    <div class="card" id="trace-list"></div>`;
  const list = container.querySelector("#trace-list");
  const events = api.state.trace.slice(-200);
  if (!events.length) { list.innerHTML = `<div class="dim">No activity yet.</div>`; return; }
  for (const e of events) {
    const div = document.createElement("div");
    const denied = e.type === "tool_decision" && e.resolution === "deny";
    div.className = "toolline" + (denied ? " deny" : "");
    div.textContent = summarize(e);
    // raw payload only in Debug
    if (e.input || e.text) {
      const pre = document.createElement("pre");
      pre.className = "mono debug-only";
      pre.textContent = JSON.stringify(e, null, 2);
      div.appendChild(pre);
    }
    list.appendChild(div);
  }
}

function summarize(e) {
  if (e.type === "tool_decision") return `decision · ${e.name} · gate=${e.gate_decision} → ${e.resolution}`;
  if (e.type === "tool_started") return `run · ${e.name}`;
  if (e.type === "tool_finished") return `done · ${e.name}${e.is_error ? " (error)" : ""}`;
  if (e.type === "text_delta") return `text · ${(e.text || "").slice(0, 60)}`;
  if (e.type === "turn_completed") return `turn complete · ${e.stop_reason}`;
  if (e.type === "subagent_event") return `sub-agent «${e.title}» · ${e.inner ? e.inner.type : ""}`;
  if (e.type === "subagent_completed") return `sub-agent «${e.title}» ${e.status}`;
  return e.type || "event";
}
