# Phase 10B — live / local verification

Phase 10B (Orchestration Studio + Team Tool Intelligence) ships behind fail-closed flags. This
is the verification checklist. It separates **what is verified here** (keyless, deterministic)
from **what requires your environment** (scanner binaries + API keys) — per amendment A6, the
second set is NOT claimed done until you run it.

## Verified here (keyless, in CI/tests)

- **Full suite green**: `uv run pytest -q` → 1260 passed, 1 skipped; `ruff check` clean;
  migrations at v8.
- **Fail-closed availability**: `services.enabled: []` ⇒ no service tool registers, zero
  services `available` (`test_service_adapters.py`, `test_service_registry.py`).
- **Adapter guards against THIS repo** (no binaries needed — the adapter's own logic):
  - B4 exclusion globs derived from `paths.py` cover `.env`, `data/connectors/`, `.ssh`, `*.pem`.
  - B4 second belt flags this repo's real `.env` and `data/connectors/…` as sensitive; normal
    source is not.
  - Call-site `context_policy`: semgrep/gitleaks refuse `.env`, `.ssh`, and a path escaping the
    project root.
  - B3 Playwright: `127.0.0.1:5173` allowed; `example.com`, cloud-metadata `169.254.169.254`,
    and `verb=click` all refused.
- **Adversarial safety pins** (`test_service_safety.py`): Plan denies service tools; Auto never
  auto-approves them; no council/review scope holds an egress/write tool; a read-only member
  never acquires an execution-stage service; scanners are non-egress + repo-confined.

## Requires YOUR environment (not run here — no binaries / no API key)

### 0. Install the local tools + set the flag

```bash
# scanners (local, free)
brew install semgrep gitleaks           # or pipx install semgrep; go install gitleaks
# playwright (inspect-only browser QA)
uv sync --extra playwright && uv run playwright install chromium
```

```yaml
# config/settings.yaml — enable ONLY the three local adapters (nothing external)
services:
  enabled: [semgrep, gitleaks, playwright_local]
  # semgrep_config: ./path/to/local/rules   # for a fully offline scan (else "auto")
  # playwright_allow_ports: [5173, 3000]     # optional narrowing (loopback host is the guarantee)
```

### 1. Studio shows the three available, everything else deferred

`jarvis --ui` → Studio. The three local services show **available**; every external/`later`
row shows disabled / missing-credentials / deferred with a reason. No key text anywhere (Hub +
Studio are presence-only). `jarvis` prints nothing secret.

### 2. Security team `security_review` on this repo

Studio → Security team → `security_review` → task brief "scan this repo". Council fans out
(semgrep + gitleaks, ALLOW, read-only) → Fable synthesis → verdict. Confirm: findings show
`file:line + rule id` (Gitleaks NEVER a secret value); the run + team-attributed model/service
costs appear in the ledger and the Costs screen (by team, by service).

### 2b. B4 canary proof

Plant a canary in a sensitive path (`echo 'SECRET=canary-XYZ' > .env.local`; a fake token under
`data/connectors/`). Re-run `security_review`. The canary appears in NO finding, and Gitleaks
findings remain `file:line + rule id` only. Remove the canary afterward.

### 3. Frontend `ux_critique` / execution playwright (inspect-only)

Run a frontend workflow with a local dev app on `127.0.0.1:<port>`. The writer's
`playwright_inspect` screenshots / DOM-inspects / a11y-checks. Confirm: a non-localhost URL is
refused by the adapter, and there is NO click/type/submit verb to invoke (B3).

### 4. Budget reservation demo

Set a tiny per-run `budget_usd` (or `per_role_max_usd`) on a run → the pre-fan-out reservation
refuses it with a clear reason (`budget_stopped`), before any member spawns.

### 5. Chunked eval gate (costs API $)

Background eval runs die ~14 min, so **chunk** it (run per-suite, aggregate once):

```bash
uv run jarvis eval gate --suite core --no-judge          # chunk 1
uv run jarvis eval gate --suite adversarial --no-judge   # chunk 2 (incl. the 2 new service scenarios)
uv run python tests/evals/runner.py --judge-only         # judge new/changed scenarios
uv run python tests/evals/runner.py --aggregate          # one PASS/FAIL verdict
```

The two new adversarial scenarios (`inj_scanner_finding_poison`, `inj_scan_target_sensitive`)
have NO baseline entry yet, so they run as measurement + basic pass (no token/judge floor). If
the aggregate is GREEN, ratchet their baselines from the real run in a dedicated commit:

```bash
uv run python tests/evals/runner.py --suite adversarial --propose-baselines
# apply the ratchet discipline (see docs/evals-baseline.md), commit baselines.yaml alone
```

Do NOT start orchestration on a red baseline; a regression stops progress until diagnosed.
