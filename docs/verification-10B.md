# Phase 10B — live / local verification

Phase 10B (Orchestration Studio + Team Tool Intelligence) ships behind fail-closed flags. This
is the verification checklist. It separates **what is verified here** (keyless, deterministic)
from **what requires your environment** (scanner binaries + API keys) — per amendment A6, the
second set is NOT claimed done until you run it.

Commands are given for **Windows PowerShell** (the primary machine); macOS/Linux equivalents are
noted inline where they differ. Run everything from the repo root.

## Verified here (keyless, in CI/tests)

- **Full suite green**: `uv run pytest -q` → 1260 passed, 1 skipped; `ruff check` clean;
  migrations at v8.
- **Fail-closed availability**: `services.enabled: []` ⇒ no service tool registers, zero
  services `available` (`test_service_adapters.py`, `test_service_registry.py`).
- **Adapter guards against THIS repo** (no binaries needed — the adapter's own logic):
  - B4 exclusion globs derived from `paths.py` cover `*.env`, `.env.*`, `data/connectors/`,
    `.ssh`, `*.pem`, `*.key`, and the rest of the sensitive floor.
  - B4 second belt flags this repo's real `.env` and `data/connectors/…` as sensitive; normal
    source is not.
  - Call-site `context_policy`: semgrep/gitleaks refuse `.env`, `.ssh`, and a path escaping the
    project root.
  - **B3 Playwright guards** (`test_service_adapters.py -k playwright`): `127.0.0.1:5173`
    allowed; `example.com`, cloud-metadata `169.254.169.254`, and `verb=click` all refused.
    This is the *complete* Playwright verification today — see the note in step 3 about why the
    real-browser path is deferred.
- **Adversarial safety pins** (`test_service_safety.py`): Plan denies service tools; Auto never
  auto-approves them; no council/review scope holds an egress/write tool; a read-only member
  never acquires an execution-stage service; scanners are non-egress + repo-confined.

## Requires YOUR environment (not run here — no binaries / no API key)

### 0. Install the local tools + set the flag

**Semgrep** (SAST):

```powershell
if (Get-Command semgrep -ErrorAction SilentlyContinue) { 'semgrep: present' } else { uv tool install semgrep; uv tool update-shell }
semgrep --version   # go/no-go, in the SAME shell you'll launch the UI from
```

> Semgrep's **native-Windows support is experimental**. If `semgrep --version` fails, run the
> whole verification inside **WSL2** (`pip install semgrep` works cleanly there) or via Docker
> (`docker run --rm -v "${PWD}:/src" semgrep/semgrep semgrep --version`); otherwise skip the
> live Semgrep portion and rely on Gitleaks + the keyless `test_service_adapters.py` coverage.
> Do not fake a pass. (macOS/Linux: `pipx install semgrep` or `brew install semgrep`.)

**Gitleaks** (secret scan) — works natively on Windows (Go binary):

```powershell
if (Get-Command gitleaks -ErrorAction SilentlyContinue) { 'gitleaks: present' } else { scoop install gitleaks }   # alt: choco install gitleaks
gitleaks version
```

> Fallback: download `gitleaks_*_windows_x64.zip` from
> `https://github.com/gitleaks/gitleaks/releases` and put `gitleaks.exe` on your `PATH`.
> (macOS/Linux: `brew install gitleaks` or `go install github.com/gitleaks/gitleaks/v8@latest`.)

**Playwright** — **not installed here.** The real-browser inspect path is deferred (there is no
`playwright` extra and no `InspectDriver` wired at runtime yet — see step 3). Nothing to install
for this checklist; the guard behavior is verified keylessly in step 3.

**Enable ONLY the three local adapters** (a **local-only** edit to a tracked file — never commit
it; step 6 reverts it):

```powershell
# The line `  enabled: []` is unique to the services: block — flip just that one line.
(Get-Content config/settings.yaml -Raw) -replace '(?m)^  enabled: \[\]', '  enabled: [semgrep, gitleaks, playwright_local]' | Set-Content config/settings.yaml -NoNewline
Select-String -Path config/settings.yaml -Pattern 'enabled: \[semgrep'   # confirm the flip
```

The enabled block should read:

```yaml
# config/settings.yaml — enable ONLY the three local adapters (nothing external)
services:
  enabled: [semgrep, gitleaks, playwright_local]
  # semgrep_config: ./path/to/local/rules   # for a fully offline scan (else "auto")
  # playwright_allow_ports: [5173, 3000]     # optional narrowing (loopback host is the guarantee)
```

### 1. Studio shows the three available, everything else deferred

`uv run jarvis --ui` → Studio. Semgrep + Gitleaks show **available**; every external/`later` row
shows disabled / missing-credentials / deferred with a reason. `playwright_local` shows
available too, but its real-browser path is inert until the driver is wired (step 3). No key text
anywhere (Hub + Studio are presence-only). `jarvis` prints nothing secret. (On the **Projects**
screen, create or select a project first — teams are project-scoped, so Studio needs one active.)

### 2. Security team `security_review` on this repo

Studio → Security team → `security_review` → task brief "scan this repo". Council fans out
(semgrep + gitleaks, ALLOW, read-only) → Fable synthesis → verdict. Confirm: findings show
`file:line + rule id` (Gitleaks NEVER a secret value); the run + team-attributed model/service
costs appear in the ledger and the Costs screen (by team, by service).

### 2b. B4 canary proof (planted only under gitignored `data/connectors/`)

Plant the canary at a path that is **both** on the sensitive floor **and** gitignored, so it can
never be committed. `data/connectors/` is the Kairo token store (a B4 exclusion), and all of
`data/` is gitignored; a `*.env` name is doubly on the floor:

```powershell
New-Item -ItemType Directory -Force -Path data/connectors | Out-Null
git check-ignore data/connectors/canary_secret.env   # MUST print the path (=> ignored). If blank, STOP.
Set-Content -Path data/connectors/canary_secret.env -Value 'AWS_SECRET_ACCESS_KEY=canary-DO-NOT-LEAK-abc123'
```

Re-run `security_review`. The canary appears in **no** finding, and Gitleaks findings remain
`file:line + rule id` only. Then remove it and confirm the tree is clean:

```powershell
Remove-Item data/connectors/canary_secret.env
git status --short   # must show NO canary file
```

### 3. Playwright — guard verification only (real-browser deferred)

The `playwright_inspect` tool ships with its safety guards fully implemented, but **no real
browser driver is wired**: `set_driver()` is only called in tests, so at runtime the tool uses
`_NotInstalledDriver` and a real `navigate`/`screenshot`/`dom_inspect` returns an error rather
than a page. Verify the guards (localhost-only, inspect-only verb set) keylessly:

```powershell
uv run pytest tests/unit/test_service_adapters.py -k playwright -q
```

This asserts a non-localhost URL is refused, cloud-metadata is refused, and there is no
`click`/`type`/`submit`/`eval` verb to invoke (B3). **Real-browser inspect against a running
localhost app is a deferred follow-up** — see "Follow-up" at the end; it is required before
Phase 11's UI-screenshot definition of done (R4).

### 4. Budget reservation demo

In Studio, set a tiny per-run `budget_usd` (e.g. `0.01`) on a Security `security_review` run →
the pre-fan-out reservation refuses it with a clear reason (`budget_stopped`), before any member
spawns.

### 5. Chunked eval gate (costs API $)

Background eval runs die ~14 min, so **chunk** it: stage each suite as its own sub-run, then
aggregate once into one gate + one history line. Judging happens *inside* each `run` (omit
`--no-judge` for a judged gate); each `run` is resumable per-scenario, so re-invoking after an
interruption skips completed scenarios.

```powershell
uv run jarvis eval run --suite core --stage data/evals/_chunked-live
uv run jarvis eval run --suite adversarial --stage data/evals/_chunked-live   # incl. the 2 new service scenarios
uv run jarvis eval aggregate --stage data/evals/_chunked-live --report
```

> One-liner alternative (resumable profile, computes its own stage dir):
> `uv run jarvis eval gate --profile live-chunked --report`.

The two new adversarial scenarios (`inj_scanner_finding_poison`, `inj_scan_target_sensitive`)
have NO baseline entry yet, so they run as measurement + basic pass (no token/judge floor). If
the aggregate is GREEN, propose their baselines from the real run and ratchet them in a dedicated
commit:

```powershell
uv run jarvis eval aggregate --stage data/evals/_chunked-live --propose-baselines
# --propose-baselines PRINTS proposed values (it does not write them). Copy the entries for the
# two new scenarios into tests/evals/baselines.yaml per docs/evals-baseline.md, then commit alone:
git add tests/evals/baselines.yaml
git commit -m "evals: ratchet baselines for the two 10B service adversarial scenarios"
```

Do NOT start orchestration on a red baseline; a regression stops progress until diagnosed.

### 6. Clean up (revert the local config change)

```powershell
git checkout -- config/settings.yaml   # restores enabled: []
git status --short                      # nothing from this checklist should remain staged/tracked
```

Never commit `config/settings.yaml`, `config/permissions.yaml`, `docs/PLAN.md`,
`docs/PLAN-7-*`, `mcp_sample.json`, or any local vault/secret. The only commit this checklist
produces is `tests/evals/baselines.yaml` (step 5), and only on a green gate.

## Follow-up (not part of 10B; scheduled for Phase 11)

**Wire a real Playwright `InspectDriver`** and add a `playwright` optional-dependency so
`playwright_inspect` can drive an actual localhost browser (navigate / screenshot / dom_inspect /
a11y_check / visual_diff), calling `set_driver()` at UI startup behind the existing flag +
localhost/verb guards. This is a prerequisite for Phase 11's UI-screenshot definition of done
(R4: empty / populated / narrow / project / search / artifact-preview captures via
`playwright_local`). Until then, the guards are verified (step 3) but no page is rendered.

## Resolved follow-up (Phase 10C T6): Semgrep default config

The 10B closeout found the shipped default `services.semgrep_config: auto` incompatible with the
adapter's hardened `--metrics=off` (`semgrep scan --config auto --metrics=off` → rc=2 "Cannot
create auto config when metrics are off"). Phase 10C T6 changes the **code default** to `p/ci`
(a curated registry pack that runs cleanly with `--metrics=off` — verified locally on
semgrep 1.168.0, rc=0). `config/settings.yaml` is unchanged (the default lives in
`config.py`); a `security_review` on a machine with the scanners now runs a real Semgrep pass
out of the box. A fully offline scan still uses a local rules directory
(`services.semgrep_config: ./path/to/rules`).
