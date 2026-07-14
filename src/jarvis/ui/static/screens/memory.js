// Memory — stored facts/preferences with provenance. Forget flips status (never DELETE).
import { el } from "../ui/dom.js";
import { openMemoryDraft } from "../ui/memory-draft.js";

export async function render(container, api) {
  const rows = await api.get("/api/memory");
  container.textContent = "";
  if (rows === null) {
    container.append(el("div", { class: "rise" }, [
      el("h1", { text: "Memory" }),
      el("div", { class: "sub", text: "Unavailable — long-term memory off." }),
    ]));
    return;
  }
  const remember = el("button", { class: "rowbtn", type: "button", text: "Remember something" });
  remember.addEventListener("click", async () => {
    if (await openMemoryDraft(api)) render(container, api);
  });
  const tbl = el("table", { id: "mem-tbl" }, [
    el("tr", {}, [
      el("th", { text: "Fact" }), el("th", { text: "Type" }), el("th", { text: "Source" }),
      el("th"),
    ]),
  ]);
  container.append(
    el("div", { class: "rise" }, [
      el("h1", { text: "Memory" }),
      el("div", { class: "sub", text: "What Kairo remembers about you — with where it came from." }),
    ]),
    el("div", { class: "card rise" }, [
      el("div", { class: "card-head" }, [el("div", { class: "t", text: "Remembered" }), remember]),
      tbl,
    ]),
  );
  if (!rows.length) {
    tbl.append(el("tr", {}, [el("td", { colspan: "4", class: "dim", text: "Nothing stored yet." })]));
    return;
  }
  for (const m of rows) {
    const actions = el("td", { class: "actions-cell" });
    const b = el("button", { class: "rowbtn", type: "button", text: "Forget" });
    b.addEventListener("click", async () => { await api.post(`/api/memory/${m.id}/forget`); render(container, api); });
    actions.append(b);
    tbl.append(el("tr", {}, [
      el("td", { text: m.content }),
      el("td", {}, [el("span", { class: "tag", text: m.type })]),
      el("td", { class: "dim", text: m.source }), actions,
    ]));
  }
}
