# Moving Kira from Windows to macOS

This guide moves one existing, single-owner Kira workplace to a new Mac. It is an **offline
transfer**, not live multi-device sync: stop every Kira process before copying state, and do not
open the same data root from two machines.

Canonical runtime state is under `data/`, including SQLite at `data/kira.db`. A complete move may
also need `.env`, reviewed configuration, an external knowledge vault, and optionally old logs.
Those items are deliberately not all bundled together because they have different trust and
portability requirements.

## 1. Prepare the Mac

Install Apple's Command Line Tools, then install Homebrew using its [official instructions][homebrew].
Use the current Homebrew formulae for `uv` and PowerShell 7:

```bash
xcode-select --install
brew install uv
brew install powershell
```

PowerShell is required even when Terminal uses zsh: Kira's shell tool deliberately executes
commands through `pwsh`. The formula is maintained by Homebrew; see Microsoft's
[supported macOS package instructions][powershell-macos], its [Homebrew guidance][powershell-brew],
and the [uv installation guide][uv-install] when choosing an installation method.

Clone the repository into a new directory, then install the UI dependencies:

```bash
uv sync --extra ui
uv run pytest -q
pwsh -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion'
```

The test suite is keyless. Add only the extras the workplace actually uses:

```bash
uv sync --extra ui --extra voice --extra docling
```

The macOS `sounddevice` wheel normally includes PortAudio. If voice-device loading specifically
reports that PortAudio is unavailable, install the system library with `brew install portaudio` and
retry; it is not a default prerequisite.

The browser inspection extra is separate; if needed, install it and its local Chromium runtime
using the commands documented in `pyproject.toml`.

## 2. Freeze and safeguard the Windows source

1. Stop the UI, terminal REPL, voice mode, and every attended maintenance command.
2. Confirm no Kira process is still using the source data root. One data root permits one owner
   process.
3. Confirm the existing owner password works. The migrated database keeps that owner account; it
   does not create a replacement enrollment grant on the Mac.
4. Run `uv run kira doctor` while Kira is stopped. Resolve any interrupted reset, database cutover,
   ambiguous identity, or integrity error on Windows before copying.
5. Keep the original machine and its state unchanged until the Mac passes verification.

Optionally create and verify a safety archive while Kira is stopped:

```powershell
uv run kira backup create
uv run kira backup verify data/backups/kira-backup-<timestamp>-manual-<id>
```

Kira backup format v2 is useful evidence that SQLite and selected state are internally consistent,
but it is **not** the migration source. It excludes `.env`, configuration, logs, and connector token
stores, and Restore is not supported. Copy the stopped data root as described below.

Do not manually rename or delete SQLite files. Offline startup can promote one real legacy
`data/jarvis.db` into `data/kira.db` and leave a small compatibility guard at the legacy path. Kira
fails closed only when both paths contain real databases, when sidecars are orphaned or conflicting,
or when cutover state is ambiguous. On first open, an older database is safeguarded and migrated
through the current schema v33.

## 3. Recreate secrets securely

Prefer recreating `.env` on the Mac from the current `.env.example`. If it must be copied, use a
trusted encrypted transfer and never place it in Git, chat, screenshots, or an ordinary shared
folder.

`ANTHROPIC_API_KEY` is required to start any interactive Kira entrypoint. Copy only the additional
credentials for features that are intentionally enabled, such as embeddings, search, worker
models, cloud voice, or connectors. Compare the old environment with the current settings and
`.env.example`; do not assume a historical key list is complete.

Connector credentials need special handling:

- Google needs `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` for a Desktop-app OAuth client.
- Telegram notifications need `TELEGRAM_BOT_TOKEN` and either `TELEGRAM_CHAT_ID` or the
  corresponding notification setting. Remote Operator separately requires a positive private
  `connectors.telegram.remote_control.allowed_chat_id`; a notification chat ID never grants inbound
  authority.
- Kakao needs `KAKAO_REST_API_KEY`. `KAKAO_CLIENT_SECRET` and the `KAKAO_REDIRECT_URI` override are
  optional, but a loopback redirect URI whose port matches `connectors.kakao.redirect_port` must
  always be registered.

## 4. Reapply configuration for the new host

Review `config/settings.yaml` rather than assuming every Windows path is portable:

- Update `paths.data_dir`, `paths.logs_dir`, `knowledge.dir`, and every entry in `connectors.repos`.
- Set `ui.enabled: true` only if the local workplace UI should start. The committed default is
  `false`.
- Keep `ui.host` on `127.0.0.1`; non-loopback UI binding is refused.
- Review provider, connector, voice, budget, and Remote Operator opt-ins before restoring secrets.

Do **not** copy `config/permissions.yaml` blindly. Start with the committed defaults, then re-grant
only the exact commands needed on the Mac. PowerShell cmdlets are cross-platform, but executable
locations, path forms, scripts, and previously approved command prefixes may be host-specific.

If the knowledge vault is outside `data/`, copy it separately while Kira is stopped and update
`knowledge.dir`. Preserve any independent vault version history using that vault's own process.

Project-linked repositories and folders are different from `connectors.repos`: their absolute paths
are stored inside the copied database. Copy or clone those external roots separately. The shipped
UI and CLI do not yet relink an existing project's repository list, so Windows-only project paths
are a known migration gap. Do not edit SQLite by hand; treat any required relink as a blocker and
keep the Windows source until a supported relink flow is available.

## 5. Transfer the stopped data root

Use a new target checkout with no active Kira process and a fresh, empty target `data/` path. If a
target data path already exists, move it aside rather than merging it with the source. Copy the
source `data/` directory as one consistent tree, but leave out `data/connectors/` so OAuth grants
are deliberately re-established on the Mac. For example, after mounting a trusted transfer volume:

```bash
rsync -a --exclude 'connectors/' '<trusted-transfer>/data/' './data/'
```

Do not merge two independently used data roots, copy only the SQLite main file, or run Syncthing,
cloud sync, or `rsync` against a live workplace. Copying the whole stopped tree keeps the database,
knowledge, artifacts, eval history, and other state below `data/` together.

Whole-instance reset manifests and quarantines are siblings of the data root, for example
`.kira-reset-manifests/` and `.data.kira-quarantine-*`; this copy does not include them. Never
transfer an instance with an interrupted reset or database cutover. Recover or complete it on the
Windows source first.

`logs/` is optional diagnostic history rather than canonical runtime state. If copied, keep it
private; new records use `logs/kira-YYYY-MM-DD.jsonl`.

Before reconnecting accounts or starting Kira normally, inspect the transferred state without
changing it:

```bash
uv run kira doctor
```

Doctor makes no network requests or local changes. An older database may report that it needs the
expected migration to schema v33; ambiguous identity, pending recovery, reset, or integrity errors
must be resolved rather than bypassed.

## 6. Reconnect external accounts

The copied `data/` intentionally has no OAuth token store. With Kira still stopped, run only the
rituals for integrations you want on the Mac:

```bash
uv run kira connect google
uv run kira connect kakao
uv run kira connect telegram --test
uv run kira connect status
```

Google's Desktop-app loopback flow displays the exact scopes being granted. Gmail remains
drafts-only with no send capability; Calendar and Kira-created Drive documents use previewed,
approval-gated write paths. Kakao prints the exact loopback redirect URI to register. Telegram's
test sends a real message, so use it only when that egress is intended.

The optional Telegram Remote Operator is a bounded companion to the running local process, not
remote browser access or a cloud wake-up service. Recheck its private-chat allowlist and opt-in
settings on the new host before enabling it. The outbound notification chat ID does not authorize
inbound Remote Operator traffic.

## 7. Verify before retiring the old machine

First run the deterministic checks:

```bash
uv run pytest -q
uv run kira eval gate
uv run kira connect status
```

Bare `kira eval gate` is keyless cassette replay and costs $0. A live gate is separate and must be
explicitly budget-capped, for example:

```bash
uv run kira eval gate --live --runs 1 --max-cost-usd 1.00
```

For a connector smoke check before granting OAuth, leave real connector credentials unset, set
`connectors.demo: true`, enable the UI, and start:

```bash
uv run kira --ui
```

Demo data is visibly badged and does not access live connector accounts. Normal model-provider
egress rules still apply, so demo mode is not an offline-model mode. If an effective live connector
credential set is configured, the live connector configuration wins rather than being masked by
demo data.

On a migrated database, sign in with the existing owner password; a browser session from Windows
does not transfer. Check Chat, Daily, Hub, Notifications, project content, knowledge, tasks, and the
canonical `data/kira.db`. Keep the old machine untouched until those checks pass.

## Platform boundaries

- The browser workplace remains loopback-only on macOS.
- Scheduler, digest, and Remote Operator work only while a Kira process is running; there is no
  shipped launchd daemon or automatic cloud wake-up.
- Voice capture needs the optional voice dependencies and may require macOS microphone permission.
- Repository CI currently covers Ubuntu and Windows, not macOS; local Doctor, tests, and the UI
  smoke check are the acceptance evidence for this move.
- Re-run permission approvals for host-specific commands instead of widening old prefixes.
- Never use consumer file sync as active-active database replication.

[homebrew]: https://docs.brew.sh/Installation
[powershell-macos]: https://learn.microsoft.com/powershell/scripting/install/install-powershell-on-macos
[powershell-brew]: https://learn.microsoft.com/powershell/scripting/install/alternate-install-methods
[uv-install]: https://docs.astral.sh/uv/getting-started/installation/
