const viewName = ({ "/setup": "setup", "/recover": "recover" })[location.pathname] || "login";
const view = document.querySelector(`[data-view="${viewName}"]`);

if (view) {
  view.hidden = false;
  document.title = `Kairo · ${viewName === "setup" ? "Create owner" : viewName === "recover" ? "Recover owner" : "Sign in"}`;
  view.querySelector("input[autofocus]")?.focus();
}

function setStatus(form, message, tone = "error") {
  const target = form.querySelector(".form-status");
  target.textContent = message;
  target.dataset.tone = tone;
}

for (const toggle of document.querySelectorAll("[data-show-password]")) {
  toggle.addEventListener("change", () => {
    const form = toggle.closest("form");
    for (const input of form.querySelectorAll('input[name="password"], input[name="confirm"]')) {
      input.type = toggle.checked ? "text" : "password";
    }
  });
}

for (const form of document.querySelectorAll("form[data-endpoint]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setStatus(form, "");
    if (!form.reportValidity()) return;

    const data = new FormData(form);
    const password = String(data.get("password") || "");
    const confirm = data.get("confirm");
    if (confirm !== null && password !== String(confirm)) {
      setStatus(form, "The passphrases do not match.");
      form.querySelector('input[name="confirm"]')?.focus();
      return;
    }

    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    setStatus(form, "Verifying locally…", "progress");
    const payload = { password };
    const username = data.get("username");
    if (username !== null) payload.username = String(username);

    try {
      const response = await fetch(form.dataset.endpoint, {
        method: "POST",
        headers: { "content-type": "application/json", "accept": "application/json" },
        body: JSON.stringify(payload),
      });
      if (response.ok) {
        setStatus(form, "Access confirmed. Opening Kairo…", "progress");
        location.replace("/");
        return;
      }
      const detail = (await response.text()).trim();
      if (response.status === 429) {
        const wait = Number(response.headers.get("retry-after")) || 30;
        setStatus(form, `Too many attempts. Try again in ${wait} seconds.`);
      } else if (response.status === 401 && viewName === "login") {
        setStatus(form, "That passphrase is not correct.");
      } else if (response.status === 401) {
        setStatus(form, "This setup link has expired. Restart Kairo for a fresh link.");
      } else {
        setStatus(form, detail || "Kairo could not complete this request.");
      }
    } catch {
      setStatus(form, "Kairo is not reachable. Check that the workstation is running.");
    } finally {
      button.disabled = false;
    }
  });
}
