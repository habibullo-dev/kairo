// Hub — a read-only, token-free guide to connector and model readiness. OAuth, notification
// tests, and disconnect remain terminal rituals until a separately reviewed safe UI flow exists.
// All dynamic data is placed through the shared DOM helper, never interpolated as HTML.
import { el } from "../ui/dom.js";

const STATE_LABELS = {
  connected: "Connected", configured: "Configured", needs_reconnect: "Needs reconnect",
  missing_key: "Missing key", disabled: "Disabled", available: "Available", deferred: "Deferred",
};

function statusChip(state) {
  const label = STATE_LABELS[state] || "Deferred";
  const tone = state === "connected" || state === "available" ? " good"
    : state === "needs_reconnect" || state === "missing_key" ? " warn" : "";
  return el("span", { class: "hub-state" + tone }, [label]);
}

function copyCommand(command) {
  const button = el("button", { class: "hub-copy", type: "button" }, ["Copy"]);
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard?.writeText(command);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Copy unavailable";
    }
  });
  return el("div", { class: "hub-command" }, [el("code", {}, [command]), button]);
}

function card(title, state, children, extraClass = "") {
  return el("section", { class: "hub-card " + extraClass }, [
    el("div", { class: "hub-card-head" }, [el("h3", {}, [title]), statusChip(state)]),
    ...children,
  ]);
}

function rule(title, text, cannot = false) {
  return el("div", { class: "hub-rule" + (cannot ? " cannot" : "") }, [
    el("span", { class: "hub-rule-label" }, [title]), el("span", {}, [text]),
  ]);
}

function googleCard(google = {}) {
  const scopes = (google.scopes || []).length
    ? el("div", { class: "hub-scope-list" }, google.scopes.map((scope) =>
      el("span", { class: "chip" }, [scope.name])))
    : el("div", { class: "hub-muted" }, ["Granted scopes appear here after Google is connected."]);
  const services = (google.services || []).map((service) => el("div", { class: "hub-service-line" }, [
    el("div", { class: "hub-service-title" }, [el("strong", {}, [service.name]), statusChip(service.state || google.state || "disabled")]),
    rule("Can", service.can), rule("Cannot", service.cannot, true),
  ]));
  return card("Google Workspace", google.state || "disabled", [
    el("p", { class: "hub-copy-text" }, ["Calendar, Gmail, and Drive share one narrow Google grant."]),
    el("div", { class: "hub-service-list" }, services),
    el("div", { class: "hub-detail-label" }, ["Granted scopes"]), scopes,
    el("div", { class: "hub-detail-label" }, ["Connect or review status in the terminal"]),
    copyCommand(google.command || "uv run jarvis connect google"),
    copyCommand(google.status_command || "uv run jarvis connect status"),
    el("p", { class: "hub-muted" }, [google.disconnect_note ||
      "Disconnect is intentionally not a UI action. Revoke Kairo in the provider account, then check status."]),
  ]);
}

function telegramCard(telegram = {}) {
  return card("Telegram", telegram.state || "disabled", [
    rule("Can", "Send one-way notifications to a configured destination."),
    rule("Cannot", "Read messages, act as a chat channel, or expose the destination ID.", true),
    rule("Destination", telegram.chat_id_set ? "Set" : "Not set"),
    el("div", { class: "hub-detail-label" }, ["Test from the terminal"]),
    copyCommand(telegram.command || "uv run jarvis connect telegram --test"),
  ]);
}

function kakaoCard(kakao = {}) {
  const nodes = [
    rule("Can", "Send a memo to your own Kakao account."),
    rule("Cannot", "Read Kakao, message other people, or expose OAuth tokens.", true),
  ];
  if (kakao.redirect_uri) {
    nodes.push(el("div", { class: "hub-detail-label" }, ["Registered loopback redirect URI"]));
    nodes.push(el("code", { class: "hub-inline-code" }, [kakao.redirect_uri]));
  }
  nodes.push(el("div", { class: "hub-detail-label" }, ["Reconnect or test from the terminal"]));
  nodes.push(copyCommand(kakao.command || "uv run jarvis connect kakao"));
  nodes.push(copyCommand(kakao.test_command || "uv run jarvis connect kakao --test"));
  return card("Kakao", kakao.state || "disabled", nodes);
}

function providerCard(provider) {
  const privacy = provider.private_ok ? "Private-context eligible" : "Worker-only; never receives private chat";
  const authority = provider.trusted_authority ? "Trusted authority" : "Not final authority";
  const selectable = provider.selectable ? "Manual chat selectable" : "Not manually selectable";
  return card(provider.name, provider.state || "deferred", [
    el("div", { class: "hub-provider-facts" }, [
      el("span", {}, [provider.enabled ? "Enabled" : "Disabled"]),
      el("span", {}, [provider.key_present ? "Key present" : "Key missing"]),
      el("span", {}, [provider.priced ? "Priced" : "Pricing deferred"]),
      el("span", {}, [selectable]), el("span", {}, [privacy]), el("span", {}, [authority]),
    ]),
    provider.note ? el("p", { class: "hub-muted" }, [provider.note]) : null,
  ].filter(Boolean), "hub-provider-card");
}

function serviceCard(service) {
  return card(service.name, service.state || "deferred", [
    el("div", { class: "hub-provider-facts" }, [
      el("span", {}, [service.kind || "tool"]),
      el("span", {}, [service.local ? "Local" : "May use network"]),
    ]),
    service.note ? el("p", { class: "hub-muted" }, [service.note]) : null,
  ].filter(Boolean), "hub-service-card");
}

function fallbackOverview(h) {
  // The screenshot harness and older hosts can still render a useful, honest shell before they
  // provide the richer read model. No capability is inferred or turned into an action here.
  const caps = h.capabilities || {};
  const capState = (name) => (caps.connectors || []).find((row) => row.name === name)?.state;
  return {
    google: { state: capState("Gmail") || "disabled" },
    telegram: { state: capState("Telegram") || "disabled" },
    kakao: { state: capState("Kakao") || "disabled" },
    providers: (caps.providers || []).map((row) => ({ id: row.name, name: row.name, state: row.state,
      enabled: row.state === "available", key_present: row.state === "available", priced: true,
      selectable: row.exposed_to_chat, private_ok: row.exposed_to_chat, trusted_authority: row.exposed_to_chat,
      note: row.reason })),
    services: (caps.services || []).map((row) => ({ name: row.name, state: row.state,
      kind: "tool", local: false, note: row.reason })),
  };
}

export async function render(container, api) {
  container.textContent = "";
  const h = await api.get("/api/hub");
  if (!h) {
    container.appendChild(el("div", { class: "rise" }, [el("h1", {}, ["Hub"]),
      el("div", { class: "sub" }, ["Connector status is unavailable."])]));
    return;
  }
  const overview = h.connector_overview || fallbackOverview(h);
  const providers = overview.providers || [];
  const services = overview.services || [];
  container.append(
    el("div", { class: "hub-hero rise" }, [
      el("div", {}, [el("div", { class: "eyebrow" }, ["Connectors"]), el("h1", {}, ["Hub"]),
        el("p", { class: "sub" }, ["A clear, read-only view of what is ready, what needs attention, and the safe way to finish setup."])]),
      el("div", { class: "hub-hero-note" }, ["No secrets shown · writes stay preview → approve → execute"]),
    ]),
    el("section", { class: "hub-boundaries rise" }, [
      el("div", {}, [el("h2", {}, ["What Kairo can do"]), el("p", {}, ["Use connected, narrowly scoped services through the existing permission and approval flow."])]),
      el("div", {}, [el("h2", {}, ["What Kairo cannot do"]), el("p", {}, ["Send Gmail, take broad Drive access, reveal credentials, or run a hidden connector action from this page."])]),
    ]),
    el("section", { class: "hub-section rise" }, [el("div", { class: "hub-section-head" }, [
      el("h2", {}, ["Accounts & notification channels"]),
      el("p", {}, ["Use the copied terminal command when a UI action is not implemented."]),
    ]), el("div", { class: "hub-grid hub-connectors" }, [googleCard(overview.google), telegramCard(overview.telegram), kakaoCard(overview.kakao)])]),
    el("section", { class: "hub-section rise" }, [el("div", { class: "hub-section-head" }, [
      el("h2", {}, ["Model providers"]),
      el("p", {}, ["Private-chat eligibility and final authority are policy states, not promises of access."]),
    ]), el("div", { class: "hub-grid hub-providers" }, providers.length
      ? providers.map(providerCard) : [el("div", { class: "hub-empty" }, ["No provider status is available."])])]),
    el("section", { class: "hub-section rise" }, [el("div", { class: "hub-section-head" }, [
      el("h2", {}, ["Services & local tools"]),
      el("p", {}, ["Availability is derived from the existing service catalog; this page does not enable anything."]),
    ]), el("div", { class: "hub-grid hub-services" }, services.length
      ? services.map(serviceCard) : [el("div", { class: "hub-empty" }, ["No service status is available."])])]),
  );
}
