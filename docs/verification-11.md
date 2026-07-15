# Phase 11 (Kairo Workstation) — verification & closeout checklist

Phase 11 is a **product-surface** phase: it adds screens over existing read models + the existing
gated routes, with **zero new authority** ([ADR-0017](decisions/0017-workstation-ui.md)). Almost
everything is verifiable keyless; the only human-machine steps are the visual screenshot pack (a
running UI + the `browser` extra) and the terminal judged eval ritual.

## Verified in CI (keyless, $0)

- **Full suite green.** `uv run pytest` — the frontend pins (no external resources, CSP, no inline
  handlers, no `innerHTML` of untrusted content), the route-closed-set pin (mutations unchanged),
  the secret-absence sweep over every non-parameterised GET, and the per-screen content pins.
- **Core replay eval gate green.** `uv run kira eval gate --suite core` → 19/19 keyless/$0 (the
  UI phase changed no agent code; the gate is a regression backstop). NOTE the bare `gate` defaults
  to `--suite all`, and the **adversarial** suite has no committed cassettes on purpose — it
  fail-closes with `CassetteMissError` (that is the cost ladder, not a regression). Use
  `--suite core` for the keyless gate.
- **Lint/type:** `uv run ruff check .` clean; `node --check` clean on every JS module.
- **No new authority (pinned, not convention):** the palette + every read screen are GET/navigate-
  only; the Artifacts/Projects/Settings writes are the enumerated metadata routes; Settings/Cost
  surfaces render presence booleans only.

## Screenshot DoD (needs a running UI + the `browser` extra — your machine)

```bash
uv sync --extra browser --extra ui && uv run playwright install chromium
uv run kira            # launch the workstation UI; it prints the one-shot token + URL
# (settings.yaml keeps ui.enabled: false by default; a small driver can override it for headless
#  capture — see the Phase 11 session notes — or set it true locally to browse.)
uv run python -m tests.ui.capture --token <TOKEN> --themes noir,light,neon \
  --screen "daily:daily:populated" --screen "projects:projects:populated" \
  --screen "artifacts:artifacts:library" --screen "costs:costs:populated" \
  --screen "settings:settings:populated" --screen "studio:studio:populated" \
  --screen "workspace/<projectId>/overview:workspace:overview" \
  --screen "workspace/<projectId>/chats:workspace:chats" \
  --screen "workspace/<projectId>/artifacts:workspace:artifacts"
```

The harness saves each `screen × theme × viewport` PNG to `data/screenshots/` (gitignored) and runs
the **no-overlap / no-horizontal-scroll** assertion (`scrollWidth ≤ innerWidth` + a bounding-rect
scan) at 1440 / 1024 / 390. Exit non-zero on any layout violation. (During Phase 11 this pack was
captured headless and came back **0 violations** across all screens × 3 themes × 3 widths.)

## Terminal ritual (needs an API key — your machine; no ratchet expected)

The judged, live, chunked eval gate stays a terminal ritual (ADR-0005) — a UI phase should not move
baselines:

```bash
uv run kira eval gate --profile live-chunked --live --max-cost-usd <cap>
```

Only ratchet baselines in a dedicated commit if this comes back green AND you intend to. No ratchet
is expected from Phase 11.

## Known follow-ups (non-blocking, agreed at Checkpoint F)

- Mobile (≤720px) rail collapses to an unlabelled icon strip — a fuller labelled mobile nav (icons
  or a drawer) is later polish.
- Chat message editing / regenerate / branching is out (11.5 fast-follow).
- The Activity feed is metadata-only and is the substrate for the Phase-14 office view.
