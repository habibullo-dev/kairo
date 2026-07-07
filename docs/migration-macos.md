# Migrating Kairo to a MacBook

Kairo is developed on Windows (PowerShell); it runs the same on macOS. This is the setup +
data-sync checklist for moving to a Mac. **All state lives under `data/`** — that plus `.env`
and `config/` is the whole migration.

## 1. Prerequisites

```bash
xcode-select --install                 # Command Line Tools (git, clang)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install uv                        # the Python/dep manager Kairo uses
# (voice extra only) brew install portaudio   # sounddevice needs it
```

Clone the repo, then:

```bash
uv sync --extra ui                     # add --extra voice --extra docling as desired
# connectors need NO extra (httpx is a core dep)
uv run pytest -q                       # should be all green, keyless
```

## 2. Secrets (`.env`) — never committed

Copy `.env` from the old machine, or recreate it. Phase 9 adds connector keys:

```
ANTHROPIC_API_KEY=...
VOYAGE_API_KEY=...            # long-term memory + KB embeddings
TAVILY_API_KEY=...            # web_search (optional)
OPENAI_API_KEY=...            # cloud voice (optional)
ELEVENLABS_API_KEY=...        # cloud voice (optional)
GOOGLE_CLIENT_ID=...          # Google connector (see §5)
GOOGLE_CLIENT_SECRET=...
TELEGRAM_BOT_TOKEN=...        # Telegram notifier (from @BotFather)
TELEGRAM_CHAT_ID=...          # your numeric chat id; precedence over settings.yaml chat_id
KAKAO_REST_API_KEY=...        # Kakao notifier
KAKAO_CLIENT_SECRET=...       # optional — only if your Kakao app enabled a client secret
KAKAO_REDIRECT_URI=...        # optional — must match http://127.0.0.1:<redirect_port> or connect fails closed
```

`TELEGRAM_CHAT_ID` in `.env` overrides `connectors.telegram.chat_id` in `settings.yaml`
(either works; the env var wins). `KAKAO_CLIENT_SECRET` is optional — leave it blank for a
PKCE-only Kakao app. If you set `KAKAO_REDIRECT_URI`, it must equal
`http://127.0.0.1:<connectors.kakao.redirect_port>` exactly (the value registered in the Kakao
Developers console) — a mismatch fails closed with a message telling you to align them.

## 3. `config/` — review before copying

- `config/settings.yaml` is safe to copy verbatim (non-secret).
- **`config/permissions.yaml`: do NOT copy blindly.** The shell prefix allow-rules were granted
  against PowerShell on Windows (`Get-ChildItem`, `dir`, …). Those commands don't exist on macOS;
  a stale allow is harmless (it just never matches) but you'll want zsh/`git`/`rg` equivalents.
  **Start from the committed defaults and re-grant interactively** — an "always allow" on the
  Mac writes the right rules. (Note: "always allow" during use rewrites this file and strips
  comments; that's expected.)

## 4. Data sync

Copy the whole `data/` directory (it's gitignored — it *is* your state):

- `data/jarvis.db` — sessions, memory, tasks, KB index, **digests**. It migrates forward
  automatically on first open (`PRAGMA user_version`; Phase 9 is schema v6).
- `data/knowledge/` — the vault (`raw/`, `markdown/`, `wiki/`). Or point `knowledge.dir` at an
  existing Obsidian vault and run `kb rebuild`.
- `data/evals/history.jsonl` — eval gate history (so the Daily eval-freshness chip is accurate).
- **`data/connectors/` — do NOT copy.** Re-run `jarvis connect google|kakao` on the Mac instead
  (§5): a fresh refresh token, and macOS actually enforces the 0600 file perms that Windows
  ignores.

A simple `cp -R`, `rsync`, or Syncthing of `data/` (minus `data/connectors/`) is the backup plan.

## 5. Reconnect the connectors (a terminal ritual)

```bash
jarvis connect google      # opens a browser; grant the 4 read-first scopes; drafts-only
jarvis connect kakao       # requires a Kakao dev app + a pre-registered redirect URI
jarvis connect telegram --test   # verifies the bot token + chat_id (a message arrives)
jarvis connect status      # shows presence + scopes + expiry (never a token value)
```

- **Google**: create an OAuth client of type **Desktop app** in the Google Cloud console; in
  "testing" mode add your own account as a test user. The loopback flow uses an ephemeral port
  (no redirect-URI registration needed for Desktop clients).
- **Kakao**: create an app, enable the `talk_message` scope, and register the redirect URI
  `http://127.0.0.1:<connectors.kakao.redirect_port>` exactly. Kakao refresh tokens expire
  (~2 months) — reconnecting is routine and the Hub flags `needs_reconnect`.

## 6. Try it without live accounts first (demo mode)

Before wiring OAuth, exercise the whole UI with clearly-badged fake data:

```yaml
# config/settings.yaml
connectors:
  demo: true
```

```bash
jarvis --ui        # open the printed tokened URL once
```

Daily's Briefing, Today, Hub connector status, and "Run digest now" all work against
`[DEMO]` data (nothing leaves the box). Demo is ignored the moment real provider keys are
present, so it can never mask a live account. This is the recommended migration smoke check.

## 7. Verify

```
uv run pytest -q                       # green
jarvis connect status                  # connectors present
uv run jarvis eval gate                # one live gate chunk (optional; costs API $)
jarvis --ui                            # open the UI, run the digest, check Daily
```

## Platform notes

- The `stream.reconfigure(encoding="utf-8")` shim (for Windows cp1252 consoles) is a harmless
  no-op on macOS.
- Voice (`--extra voice`) needs `portaudio` via brew for `sounddevice`.
- The loopback UI is unchanged (127.0.0.1 only). Remote/mobile access is still out of scope.
- Digests fire only while a Kairo process is running — there is no background daemon this phase.
