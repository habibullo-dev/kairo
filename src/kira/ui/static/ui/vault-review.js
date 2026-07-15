// Shared client ownership for Vault approve/reject writes. The same source can be rendered by the
// global Knowledge route and the project Workspace route, so neither screen may own single-flight
// state by itself. The server remains authoritative; this module only prevents duplicate/opposing
// browser submissions and fans settlement out to whichever same-authority row is currently live.

const operations = new Map();
// A route render is bounded to 15 seconds by the shell. Retain a settlement for twice that long so
// a renderer which already captured the pre-write queue can attach to the outcome after its old row
// was detached. The timer bounds module/DOM closure retention when the source correctly disappears.
const SETTLED_OUTCOME_TTL_MS = 30000;

function authorityToken(api) {
  return typeof api.authorityToken === "function" ? api.authorityToken() : null;
}

function authorityIsCurrent(api, token) {
  return token === null || typeof api.authorityIsCurrent !== "function"
    || api.authorityIsCurrent(token);
}

function normalizedScope(projectId) {
  return projectId == null ? "global" : `project-${String(projectId)}`;
}

function operationKey(api, projectId, sourceId) {
  return JSON.stringify([
    authorityToken(api), normalizedScope(projectId), String(sourceId),
  ]);
}

function discardOperation(operation) {
  if (!operation) return;
  if (operations.get(operation.key) === operation) operations.delete(operation.key);
  if (operation.expiryTimer !== null) clearTimeout(operation.expiryTimer);
  operation.expiryTimer = null;
  operation.bindings.clear();
}

export function pendingVaultReview(api, projectId, sourceId) {
  const operation = operations.get(operationKey(api, projectId, sourceId));
  return operation && authorityIsCurrent(api, operation.authorityToken) ? operation : null;
}

export function beginVaultReview(api, projectId, sourceId, action) {
  const key = operationKey(api, projectId, sourceId);
  const existing = operations.get(key);
  if (existing?.state === "pending") return null;
  // A visible failed row may be retried immediately; beginning the new attended action consumes
  // its remembered outcome rather than leaving an apparently-enabled button inert until expiry.
  if (existing) discardOperation(existing);
  const operation = {
    key,
    api,
    projectId,
    sourceId: String(sourceId),
    action,
    authorityToken: authorityToken(api),
    bindings: new Set(),
    state: "pending",
    result: null,
    expiryTimer: null,
  };
  if (!authorityIsCurrent(api, operation.authorityToken)) return null;
  operations.set(key, operation);
  return operation;
}

// A replacement row binds to pending work or consumes a retained settlement. The callback itself
// verifies DOM ownership before painting, so obsolete bindings are harmless.
export function bindVaultReview(operation, binding) {
  if (!operation || typeof binding !== "function") return false;
  if (operation.state === "settled") {
    const result = operation.result;
    // Cards bind before they are appended. Deliver on the next microtask so a late replacement row
    // is connected and can either show the failure or schedule the post-success authoritative read.
    discardOperation(operation);
    void Promise.resolve().then(() => binding({
      pending: false, action: operation.action, result,
    })).catch(() => {});
    return true;
  }
  if (operation.state !== "pending" || operations.get(operation.key) !== operation) return false;
  operation.bindings.add(binding);
  binding({ pending: true, action: operation.action, result: null });
  return true;
}

export async function settleVaultReview(operation, result) {
  if (!operation || operations.get(operation.key) !== operation) return false;
  if (!authorityIsCurrent(operation.api, operation.authorityToken)) {
    discardOperation(operation);
    return false;
  }
  operation.state = "settled";
  operation.result = result;
  operation.expiryTimer = setTimeout(() => discardOperation(operation), SETTLED_OUTCOME_TTL_MS);
  const bindings = [...operation.bindings];
  operation.bindings.clear();
  let handledByLiveBinding = false;
  for (const binding of bindings) {
    try {
      const handled = binding({ pending: false, action: operation.action, result });
      if (handled === true) handledByLiveBinding = true;
      else if (handled && typeof handled.catch === "function") void handled.catch(() => {});
    } catch { /* one obsolete surface cannot prevent shared recovery */ }
  }
  // In the passive same-route gap the original screen container is still connected even though
  // every row is detached. Start a post-write read from that route facade. Cross-surface navigation
  // disconnects this facade; the retained outcome above is then consumed by any stale late row.
  if (result?.ok && !handledByLiveBinding
      && authorityIsCurrent(operation.api, operation.authorityToken)
      && typeof operation.api.refreshRoute === "function") {
    try {
      const refresh = operation.api.refreshRoute();
      if (refresh && typeof refresh.catch === "function") void refresh.catch(() => {});
    } catch { /* late binding remains the recovery path */ }
  }
  return true;
}
