// Project source tree — a read-only view of the user-selected logical upload names.
// It does not receive origin paths, managed paths, or source bodies.  Every label is installed
// with textContent so an uploaded filename can never become markup.

function sourceParts(source) {
  const raw = String((source && source.title) || "Untitled source").replaceAll("\\", "/");
  const parts = raw.split("/").filter((part) => part && part !== "." && part !== "..");
  return parts.length ? parts : ["Untitled source"];
}

export function buildSourceTree(sources) {
  const root = { folders: new Map(), files: [] };
  for (const source of Array.isArray(sources) ? sources : []) {
    const parts = sourceParts(source);
    let node = root;
    for (const part of parts.slice(0, -1)) {
      if (!node.folders.has(part)) node.folders.set(part, { folders: new Map(), files: [] });
      node = node.folders.get(part);
    }
    node.files.push({ source, name: parts.at(-1) });
  }
  return root;
}

function sortedFolders(node) {
  return [...node.folders.entries()].sort(([a], [b]) => a.localeCompare(b));
}

function sourceMeta(source) {
  const state = source.review_status === "reviewed" ? "Ready" : "Needs review";
  return [state, source.kind || "file"].join(" · ");
}

function renderNode(node, { root = false } = {}) {
  const list = document.createElement("ul");
  list.className = root ? "source-tree source-tree-root" : "source-tree";
  for (const [name, child] of sortedFolders(node)) {
    const item = document.createElement("li");
    item.className = "source-tree-folder";
    const details = document.createElement("details");
    details.open = true;
    const summary = document.createElement("summary");
    summary.textContent = name;
    details.append(summary, renderNode(child));
    item.appendChild(details);
    list.appendChild(item);
  }
  for (const file of [...node.files].sort((a, b) => a.name.localeCompare(b.name))) {
    const item = document.createElement("li");
    item.className = "source-tree-file";
    const name = document.createElement("span");
    name.textContent = file.name;
    const meta = document.createElement("small");
    meta.textContent = sourceMeta(file.source);
    item.append(name, meta);
    list.appendChild(item);
  }
  return list;
}

export function renderSourceTree(sources, options = {}) {
  return renderNode(buildSourceTree(sources), { root: options.root !== false });
}
