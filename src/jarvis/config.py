"""Configuration: non-secret settings from YAML + secrets from the environment.

Two sources with different trust levels are deliberately kept apart:

* **Secrets** (API keys) come from ``.env`` / the process environment via
  ``pydantic-settings`` and are never written to disk by us or committed.
* **Settings** (model IDs, limits, paths) come from ``config/settings.yaml``,
  are safe to commit, and each has a code default so the app runs if the file
  is absent.

``load_config`` assembles both into a single :class:`Config`. Missing *required*
keys fail fast with an actionable message (via :meth:`Config.require`) rather
than surfacing later as an opaque 401 from the API.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# service name -> (Secrets attribute, env var name) for require() error messages.
# For a multi-key service (Google needs id AND secret), the representative key drives the
# fast-fail here; the connect ritual (Phase 9) checks the full set with its own message.
_REQUIRED_KEYS: dict[str, tuple[str, str]] = {
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "voyage": ("voyage_api_key", "VOYAGE_API_KEY"),
    "tavily": ("tavily_api_key", "TAVILY_API_KEY"),
    "openai": ("openai_api_key", "OPENAI_API_KEY"),  # cloud STT (Phase 7, opt-in)
    "elevenlabs": ("elevenlabs_api_key", "ELEVENLABS_API_KEY"),  # cloud TTS (Phase 7, opt-in)
    "deepseek": ("deepseek_api_key", "DEEPSEEK_API_KEY"),  # provider worker (Phase 10C)
    "qwen": ("dashscope_api_key", "DASHSCOPE_API_KEY"),  # Qwen via Alibaba DashScope (Phase 10C)
    "zai": ("zai_api_key", "ZAI_API_KEY"),  # GLM / Z.ai worker (Phase 10C)
    "gemini": ("gemini_api_key", "GEMINI_API_KEY"),  # Gemini text-only worker (Phase 10C)
    "google": ("google_client_id", "GOOGLE_CLIENT_ID"),  # Workspace connectors (Phase 9)
    "telegram": ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),  # send-only notifier (Phase 9)
    "kakao": ("kakao_rest_api_key", "KAKAO_REST_API_KEY"),  # send-to-me notifier (Phase 9)
}


class ConfigError(RuntimeError):
    """Configuration is missing or invalid; the message is meant for the user."""


def project_root() -> Path:
    """Repo root: ``src/jarvis/config.py`` -> ``parents[2]``.

    Works under an editable install (the default here) because ``__file__``
    still points into the source tree.
    """
    return Path(__file__).resolve().parents[2]


class Secrets(BaseSettings):
    """API keys, read from the environment / ``.env``.

    All optional at load time so config can be built without keys (unit tests,
    the tasks that don't touch the network). Presence is enforced on demand via
    :meth:`Config.require` at the point a key is actually needed.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    anthropic_api_key: str = ""
    voyage_api_key: str = ""
    tavily_api_key: str = ""
    openai_api_key: str = ""  # cloud STT (Phase 7 voice, opt-in)
    elevenlabs_api_key: str = ""  # cloud TTS (Phase 7 voice, opt-in)
    # Phase 10C direct model providers (opt-in workers). Keys ONLY here — never settings.yaml,
    # never hardcoded. gemini_api_key is DISTINCT from the Phase 9 google_client_* connector keys.
    deepseek_api_key: str = ""
    dashscope_api_key: str = ""  # Qwen (Alibaba DashScope)
    zai_api_key: str = ""  # GLM / Z.ai
    gemini_api_key: str = ""
    # Connectors (Phase 9). Google OAuth client (Desktop app); Telegram bot token; Kakao
    # REST API key. All optional; the connect ritual + Config.require enforce presence.
    google_client_id: str = ""
    google_client_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""  # optional: overrides connectors.telegram.chat_id (settings.yaml)
    kakao_rest_api_key: str = ""
    kakao_client_secret: str = ""  # optional: only if the Kakao app enabled a client secret


class ModelsConfig(BaseModel):
    """Model IDs per role. Quality-first — see config/settings.yaml.

    ``main``/``utility``/``judge``/``embedding`` are the pre-Phase-10 flat roles (still the
    live wiring). ``routes`` (Phase 10) is the settings-level override map for the role/route
    registry — ``{role: {provider, model, effort?, text_only?}}`` — layered over the code
    defaults and under per-project / per-run overrides (see jarvis.models.registry)."""

    main: str = "claude-opus-4-8"
    utility: str = "claude-sonnet-5"
    judge: str = "claude-opus-4-8"
    embedding: str = "voyage-3-large"
    routes: dict[str, dict] = Field(default_factory=dict)


class ProvidersConfig(BaseModel):
    """Direct model-provider enablement (Phase 10C). ``enabled`` is the opt-in flag list for the
    non-core providers (deepseek / qwen / zai / gemini) — a provider is usable only if it appears
    here AND its key is present AND it has ≥1 priced model (fail closed; anthropic/openai are
    core and never gated by this list). ``base_urls`` optionally overrides a provider's default
    endpoint. Empty by default: byte-identical to pre-10C behavior."""

    enabled: list[str] = Field(default_factory=list)
    base_urls: dict[str, str] = Field(default_factory=dict)


class LimitsConfig(BaseModel):
    """Loop, tool, and context guardrails."""

    max_iterations: int = 25
    tool_timeout_seconds: float = 60.0
    max_tool_result_chars: int = 24_000
    max_read_bytes: int = 1_000_000  # hard ceiling on a single file read (memory safety)
    max_dir_entries: int = 1_000  # cap on list_dir / glob_search output size
    context_token_budget: int = 180_000
    compaction_threshold: float = 0.7
    max_output_tokens: int = 32_000  # streaming, so well under the 128k cap; room for thinking
    effort: str = "high"  # output_config effort: low|medium|high|xhigh|max (quality-first default)
    max_retries: int = 4  # SDK retries 429/5xx with exponential backoff


class MemoryConfig(BaseModel):
    """Long-term memory (Phase 2) knobs. Thresholds are embedding-model-specific
    (tuned for voyage-3-large) — expect to adjust from real recall logs."""

    enabled: bool = True
    top_k: int = 6  # how many memories auto-recall may inject
    min_similarity: float = 0.35  # cosine floor for a recall hit
    dedup_trigger: float = 0.85  # cosine above which remember() adjudicates dup/supersede
    reflection: bool = True  # end-of-session distillation into long-term memory


class SchedulerConfig(BaseModel):
    """Tasks & scheduling (Phase 3) knobs — reminders and unattended background jobs."""

    enabled: bool = True
    misfire_grace_seconds: int = 3600  # due-jobs older than this on catch-up are 'missed'
    max_consecutive_failures: int = 3  # recurring job flips to 'failed' after this many errors
    wake_cap_seconds: int = 30  # loop re-checks at least this often (survives laptop sleep)
    max_job_iterations: int = 15  # unattended runaway bound (< limits.max_iterations)
    reflect_job_sessions: bool = False  # unattended transcripts don't feed memory by default
    # Tools whose policy-level ALLOW survives the unattended demotion (see D2 in
    # docs/PLAN-3-tasks.md). Empty by default: interactive grants are not unattended grants.
    unattended_allow_tools: list[str] = Field(default_factory=list)


class KnowledgeConfig(BaseModel):
    """Research + knowledge base (Phase 4) knobs. Embeddings reuse ``models.embedding``
    and the shared Embedder — no new model or secret. Thresholds are
    embedding-model-specific (voyage-3-large); tune from real retrieval logs."""

    enabled: bool = True
    # raw/ markdown/ wiki/ live under here (relative to root unless absolute). Point at
    # a git-versioned dir — or an existing Obsidian vault — to version your wiki; note
    # the default is under the gitignored data/.
    dir: Path = Path("data/knowledge")
    pdf_converter: str = "markitdown"  # 'docling' needs `uv sync --extra docling`
    chunk_chars: int = 2000  # max chunk size; heading-aware splitter
    min_chunk_chars: int = 200  # tiny sections merge forward
    top_k: int = 8  # query_knowledge_base default
    min_similarity: float = 0.30  # cosine floor for a chunk hit
    # Raw-artifact cap. Larger than limits.max_read_bytes because binary docs (PDFs,
    # decks) routinely exceed the text-read ceiling; the converted OUTPUT still flows
    # through the normal markdown/chunk/result caps, and zip members are ALSO capped
    # uncompressed in the converter (a small archive can decompress to gigabytes).
    max_ingest_bytes: int = 50_000_000
    convert_timeout_seconds: float = 120.0  # subprocess wall-clock kill (Docling is slow on CPU)


class SubAgentsConfig(BaseModel):
    """Multi-agent delegation (Phase 6) knobs — the parent can spawn scoped sub-agents.

    A sub-agent is one scoped ``AgentLoop`` turn. Every spawn is human-approved (never
    "always"-able), children can't spawn (depth 1), and children can't run unattended.
    See docs/PLAN-6-multi-agent.md and ADR-0006."""

    enabled: bool = True
    # None => inherit models.main (the default; quality-first — a child runs on the same
    # capable model as the parent, never a downgraded "cheap child" tier). Set a string
    # to pin the child model explicitly; model routing is config, not model-controllable.
    model: str | None = None
    max_iterations: int = 15  # a child is bounded tighter than the interactive parent's 25
    timeout_seconds: float = 600.0  # wall-clock per child run, enforced by the service itself
    max_parallel: int = 4  # concurrency semaphore (a resource bound, NOT a safety bound)
    # Runaway/UX guard: cap spawn calls within one parent turn so a model gone sideways
    # can't bury the terminal under approval prompts. NOT a safety bound — the human
    # approving each spawn is the safety rate limiter (ADR-0006).
    max_spawn_calls_per_turn: int = 8


#: Providers that send data off-device — reachable only with voice.cloud_providers opt-in.
_CLOUD_STT: frozenset[str] = frozenset({"openai"})
_CLOUD_TTS: frozenset[str] = frozenset({"openai", "elevenlabs"})


class VoiceConfig(BaseModel):
    """Voice interface (Phase 7). Push-to-talk MVP; wake-word activation deferred. The
    safety floor is docs/PLAN-7-voice-permissions-checkpoint.md; design in docs/PLAN-7-voice.md
    and ADR-0007. Read-only by default; risky actions escalate to on-screen confirmation
    (never voice-only); transcribed audio is untrusted; no unattended mic."""

    enabled: bool = False  # opt-in surface; off => byte-identical to Phase 6
    # Third-party (cloud) STT/TTS send audio / spoken text off-device. They are reachable
    # ONLY behind this explicit opt-in (ADR-0007); with it off, voice uses local providers.
    cloud_providers: bool = False
    stt_provider: str = "local"  # 'local' (faster-whisper) | 'openai' (cloud; needs opt-in)
    # 'local' (dep-free) | 'openai' (MVP cloud voice) | 'elevenlabs' (deferred premium). The
    # cloud choices need the opt-in; OpenAI covers both STT and TTS with one key.
    tts_provider: str = "local"
    tts_voice: str | None = None  # provider-specific voice id (None/blank = provider default)
    # DEFERRED: the wake contract is designed + tested, but activation is unwired in the MVP
    # (push-to-talk only) unless explicitly enabled later. A non-empty value does NOT turn
    # wake on by itself in this phase. None/blank in yaml is fine (like sub_agents.model).
    wake_word: str | None = None
    retain_audio: bool = False  # default: keep the transcript (untrusted), discard raw audio
    endpoint_silence_seconds: float = 0.8  # push-to-talk: silence that ends an utterance
    long_turn_ack_seconds: float = 1.5  # speak a brief "working on it" ack past this
    # Play the synthesized audio through the speakers. OFF by default (subtitle-only). When
    # on, it plays ONLY the bytes the renderer synthesized from its safe caption text —
    # never raw model output or a risky-action payload (the renderer is the sole source).
    play_audio: bool = False

    @model_validator(mode="after")
    def _cloud_requires_optin(self) -> VoiceConfig:
        """A cloud provider selection is refused unless cloud_providers is explicitly set —
        no audio or spoken text leaves the machine to a third party by accident (ADR-0007)."""
        if not self.cloud_providers:
            if self.stt_provider in _CLOUD_STT:
                raise ValueError(
                    f"voice.stt_provider '{self.stt_provider}' sends audio off-device; "
                    "set voice.cloud_providers: true to opt in"
                )
            if self.tts_provider in _CLOUD_TTS:
                raise ValueError(
                    f"voice.tts_provider '{self.tts_provider}' sends spoken text off-device; "
                    "set voice.cloud_providers: true to opt in"
                )
        return self


#: Loopback hosts the workstation UI may bind to. Anything else is refused at load —
#: a web surface reachable off-box needs TLS + real identity (a future phase), never a
#: YAML edit (ADR-0008). Fail-closed: the private-admin-console contract starts here.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


class UIConfig(BaseModel):
    """Workstation UI (Phase 8). An authenticated *local* peer of the REPL/voice — it drives
    the same AgentLoop through the same seams (events out, injected Approver in) and adds no
    new authority. Safety floor: docs/PLAN-8-ui.md + ADR-0008. Off by default; on, it binds
    loopback only, mints a per-launch token, and treats approvals as the crown jewels."""

    enabled: bool = False  # opt-in surface; off => byte-identical to Phase 7
    # Loopback ONLY this phase. A non-loopback host is a config error, not a bigger
    # allowlist — remote access needs a real auth story (ADR-0008), deferred.
    host: str = "127.0.0.1"
    port: int = 8787
    # Liveness window: an approval (and the voice screen) is resolvable only from a client
    # whose WebSocket has sent a heartbeat within this many seconds — a cookie replay from a
    # dead client cannot approve (D2/D7). Kept short; the client pings well inside it.
    heartbeat_seconds: float = 15.0
    ring_buffer_events: int = 2000  # Trace screen scrollback (bounded; oldest dropped)

    @model_validator(mode="after")
    def _host_must_be_loopback(self) -> UIConfig:
        """Refuse a non-loopback bind. The UI's authority is TTY-equivalent and local; a
        port reachable off-box would be a remote-approval channel wearing a product skin."""
        if self.host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"ui.host '{self.host}' is not loopback; the workstation UI binds "
                f"{sorted(_LOOPBACK_HOSTS)} only this phase (remote access is a future phase "
                "with TLS + real identity, not a config edit)"
            )
        return self


class GoogleConnectorConfig(BaseModel):
    """Google Workspace connector (Phase 9): read Calendar/Gmail/Drive + create Gmail drafts
    (never send). Off by default; needs GOOGLE_CLIENT_ID/SECRET in .env and a one-time
    ``jarvis connect google``. Scopes are read-only + gmail.compose (ADR-0009)."""

    enabled: bool = False
    calendar_id: str = "primary"
    max_results: int = 25  # default page size cap for list calls (tools clamp their own ≤50)


class TelegramConfig(BaseModel):
    """Telegram send-only notifier (Phase 9). Bot token in .env (TELEGRAM_BOT_TOKEN); chat_id
    here (a routing id, NOT a secret). Kairo never reads Telegram — delivery only."""

    enabled: bool = False
    chat_id: str = ""  # numeric chat id as a string; a routing id, safe to commit
    notify_reminders: bool = False  # mirror scheduler reminders here (host code, not a tool)


class KakaoConfig(BaseModel):
    """KakaoTalk "send to me" (memo) notifier (Phase 9). REST API key in .env
    (KAKAO_REST_API_KEY); OAuth via ``jarvis connect kakao`` (talk_message scope). Kakao
    requires a pre-registered redirect URI, so the loopback port is fixed, not ephemeral."""

    enabled: bool = False
    redirect_port: int = 8788  # MUST match the redirect URI registered in the Kakao dev console


def resolve_telegram_chat_id(config: Config) -> str:
    """The effective Telegram chat id: the ``TELEGRAM_CHAT_ID`` secret if set, else the
    ``connectors.telegram.chat_id`` from settings.yaml (fallback). A routing id, not a secret,
    but surfaced only as a presence boolean in status."""
    return config.secrets.telegram_chat_id or config.connectors.telegram.chat_id


def resolve_kakao_redirect_uri(config: Config) -> str:
    """The effective Kakao redirect URI — the EXACT loopback URI to register in the Kakao
    developer console. Loaded only from env/config (never hardcoded, never a secret).

    Unset ``KAKAO_REDIRECT_URI`` ⇒ the port-derived ``http://127.0.0.1:<redirect_port>``. If set,
    it is used VERBATIM — a path is allowed (e.g. ``/oauth/kakao/callback``) — after validating
    that it is a loopback ``http://`` URI whose PORT equals ``connectors.kakao.redirect_port``. A
    port mismatch or a non-loopback host fails closed (:class:`ConfigError`) naming the exact URI
    to register, because a wrong redirect silently breaks OAuth (``redirect_uri_mismatch``). A bare
    ``http://127.0.0.1:<port>/`` (path just ``/``) collapses to the derived form."""
    import os
    from urllib.parse import urlsplit

    port = config.connectors.kakao.redirect_port
    derived = f"http://127.0.0.1:{port}"
    env_uri = (os.getenv("KAKAO_REDIRECT_URI") or "").strip()
    if not env_uri:
        return derived
    parsed = urlsplit(env_uri)
    if parsed.scheme != "http" or (parsed.hostname or "") not in {"127.0.0.1", "localhost"}:
        raise ConfigError(
            f"KAKAO_REDIRECT_URI ({env_uri}) must be a loopback http:// URI (host 127.0.0.1) — "
            "Kakao redirects to a local one-shot listener."
        )
    if parsed.port != port:
        raise ConfigError(
            f"KAKAO_REDIRECT_URI port ({parsed.port}) does not match "
            f"connectors.kakao.redirect_port ({port}). Register '{env_uri}' in the Kakao developer "
            f"console AND set connectors.kakao.redirect_port to {parsed.port}, or unset "
            f"KAKAO_REDIRECT_URI to use {derived}."
        )
    return derived if parsed.path in {"", "/"} else env_uri


class DigestConfig(BaseModel):
    """Daily Digest (Phase 9): deterministic collectors + one tool-less summarize, delivered
    calmly (ADR-0010). Off by default. ``deliver`` is a subset of {ui, telegram, kakao};
    a channel requires its notifier enabled (fail-closed in ConnectorsConfig)."""

    enabled: bool = False
    cron: str = "0 8 * * *"  # local-tz 5-field crontab; validated by the scheduler's triggers
    deliver: list[str] = Field(default_factory=lambda: ["ui"])
    rich_notify: bool = False  # notifiers get headers/counts by default; opt in to snippets
    max_notify_chars: int = 3500


#: Delivery channels a digest may target. "ui" is always available (DB + NoticeBoard + WS).
_DIGEST_CHANNELS: frozenset[str] = frozenset({"ui", "telegram", "kakao"})


class ConnectorsConfig(BaseModel):
    """External connectors (Phase 9) — narrow, audited adapters behind the PermissionGate.
    All off by default. ``demo: true`` populates Daily/digest/Hub with clearly-badged fake
    data for UI testing / screenshots / migration checks (and only when the real provider
    keys are absent, so demo can never mask a live connection — enforced at wiring time)."""

    demo: bool = False
    google: GoogleConnectorConfig = Field(default_factory=GoogleConnectorConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    kakao: KakaoConfig = Field(default_factory=KakaoConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    # Repos whose git state feeds the Daily "what changed" card + the digest (read-only).
    repos: list[str] = Field(default_factory=lambda: ["."])

    @model_validator(mode="after")
    def _digest_delivery_requires_notifier(self) -> ConnectorsConfig:
        """A digest delivery channel must name a known channel AND (for telegram/kakao) have
        that notifier enabled — fail closed so a digest can't silently target a dead sink."""
        for channel in self.digest.deliver:
            if channel not in _DIGEST_CHANNELS:
                raise ValueError(
                    f"connectors.digest.deliver has unknown channel '{channel}'; "
                    f"allowed: {sorted(_DIGEST_CHANNELS)}"
                )
            if channel == "telegram" and not self.telegram.enabled:
                raise ValueError(
                    "connectors.digest.deliver includes 'telegram' but "
                    "connectors.telegram.enabled is false"
                )
            if channel == "kakao" and not self.kakao.enabled:
                raise ValueError(
                    "connectors.digest.deliver includes 'kakao' but "
                    "connectors.kakao.enabled is false"
                )
        return self


class PathsConfig(BaseModel):
    """Filesystem locations, relative to the project root unless absolute."""

    data_dir: Path = Path("data")
    logs_dir: Path = Path("logs")


class ModesConfig(BaseModel):
    """Run modes (Phase 10). ``auto_allow_tools`` is the opt-in allowlist Auto mode may
    auto-approve — empty by default (Auto adds no standing authority until a human lists
    tools). run_shell / write_file can never be added (enforced in code, not config)."""

    default: str = "approval"  # plan | approval | auto — the mode a fresh surface starts in
    auto_allow_tools: list[str] = Field(default_factory=list)


class ServicesConfig(BaseModel):
    """Team service enablement (Phase 10B). ``enabled`` is the global opt-in flag list — a
    service is usable only if it appears here AND has a "now" adapter AND its credentials +
    pricing check out (fail closed). Empty by default: no external service is on until a human
    lists it. Per-project narrowing lives in the project's settings_json["services"]."""

    enabled: list[str] = Field(default_factory=list)
    #: Semgrep ruleset passed to ``--config`` (Task 16; default fixed in Phase 10C T6). ``p/ci``
    #: is a curated registry pack that works with the adapter's hardened ``--metrics=off``; the
    #: old default ``auto`` REQUIRES metrics on and hard-errors with them off (rc=2 — verified
    #: during the 10B live closeout). Point at a LOCAL rules dir for a fully offline scan. Rules
    #: are fetched, not repo data — with ``--metrics=off`` semgrep never sends code off-box, so
    #: the tool stays non-egress.
    semgrep_config: str = "p/ci"
    #: Optional loopback ports the ``playwright_inspect`` tool may target (B3). Empty ⇒ any
    #: loopback port (still non-egress — the host allowlist is the guarantee).
    playwright_allow_ports: list[int] = Field(default_factory=list)


class ContextReuseConfig(BaseModel):
    """Provider-agnostic prompt/context caching enable-step (Phase 13, S7). OFF by default:
    with the flag off, every model request is byte-identical to a no-caching build (so replay
    cassettes stay deterministic and recordings never embed a cache control). When on, the LIVE
    Anthropic/OpenAI clients attach the S7-derived control (a ``cache_control`` breakpoint at the
    stable/volatile seam for Anthropic; a ``prompt_cache_key`` for OpenAI) to the STABLE,
    NON-SENSITIVE system prefix only — never the volatile/private tail (memory recall, connector
    data). Caching never widens data-flow (ADR-0018): it is an orthogonal, opt-in add-on."""

    enabled: bool = False


class BudgetsConfig(BaseModel):
    """Cost budgets + ROI inputs (Phase 10). 0 / None means "no limit". Per-run limits gate
    an orchestration run's accumulated spend; project_monthly caps month-to-date per project;
    confirm_above triggers a two-step confirm before an expensive run. Project overrides live
    in projects.settings_json['budgets']."""

    soft_warn_usd_per_run: float = 1.0
    hard_stop_usd_per_run: float = 5.0
    project_monthly_usd: float | None = None
    per_role_max_usd: float | None = None
    confirm_above_usd: float = 2.0
    max_rounds: int = 3  # orchestration revise-loop cap (10B)
    hourly_rate_usd: float = 100.0  # ROI: the human-time value a workflow saves
    treat_unpriced_as_blocking: bool = True  # refuse orchestration on unpriced role models


class Config(BaseModel):
    """Fully assembled configuration passed through the app."""

    root: Path
    models: ModelsConfig
    limits: LimitsConfig
    # Phase 2/3/4/6; default_factory keeps direct Config(...) callers simple.
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    sub_agents: SubAgentsConfig = Field(default_factory=SubAgentsConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    connectors: ConnectorsConfig = Field(default_factory=ConnectorsConfig)  # Phase 9
    modes: ModesConfig = Field(default_factory=ModesConfig)  # Phase 10
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)  # Phase 10
    services: ServicesConfig = Field(default_factory=ServicesConfig)  # Phase 10B
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)  # Phase 10C
    context_reuse: ContextReuseConfig = Field(default_factory=ContextReuseConfig)  # Phase 13 (S7)
    paths: PathsConfig
    secrets: Secrets

    def _abs(self, p: Path) -> Path:
        return p if p.is_absolute() else (self.root / p)

    @property
    def data_dir(self) -> Path:
        return self._abs(self.paths.data_dir)

    @property
    def logs_dir(self) -> Path:
        return self._abs(self.paths.logs_dir)

    @property
    def knowledge_dir(self) -> Path:
        return self._abs(self.knowledge.dir)

    def ensure_dirs(self) -> None:
        """Create runtime directories. A side effect, so the app calls it
        explicitly at startup — not during :func:`load_config` (keeps loading
        pure and test-safe)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def require(self, *services: str) -> None:
        """Fail fast if any named service's API key is unset.

        ``services`` are keys of :data:`_REQUIRED_KEYS` (e.g. ``"anthropic"``).
        Raises :class:`ConfigError` listing the missing env vars.
        """
        missing = [
            env
            for svc in services
            for attr, env in [_REQUIRED_KEYS[svc]]
            if not getattr(self.secrets, attr)
        ]
        if missing:
            raise ConfigError(
                "Missing required API key(s): "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill them in."
            )


def load_config(
    root: Path | None = None,
    *,
    settings_file: str = "config/settings.yaml",
    env_file: str | None = ".env",
    require: tuple[str, ...] = (),
) -> Config:
    """Load settings YAML + environment secrets into a :class:`Config`.

    ``root`` defaults to the detected project root; tests pass a temp dir.
    ``env_file`` is resolved relative to ``root``; pass ``None`` to read secrets
    from the process environment only (used by isolated tests). ``require`` names
    services whose keys must be present, enforced after loading.
    """
    root = (root or project_root()).resolve()

    settings_path = root / settings_file
    data: dict = {}
    if settings_path.exists():
        loaded = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{settings_path} must contain a YAML mapping at the top level.")
        data = loaded

    env_path = (root / env_file) if env_file else None
    env_arg = str(env_path) if env_path and env_path.exists() else None
    secrets = Secrets(_env_file=env_arg)  # type: ignore[call-arg]

    try:
        config = Config(
            root=root,
            models=ModelsConfig(**data.get("models", {})),
            limits=LimitsConfig(**data.get("limits", {})),
            memory=MemoryConfig(**data.get("memory", {})),
            scheduler=SchedulerConfig(**data.get("scheduler", {})),
            knowledge=KnowledgeConfig(**data.get("knowledge", {})),
            sub_agents=SubAgentsConfig(**data.get("sub_agents", {})),
            voice=VoiceConfig(**data.get("voice", {})),
            ui=UIConfig(**data.get("ui", {})),
            connectors=ConnectorsConfig(**data.get("connectors", {})),
            paths=PathsConfig(**data.get("paths", {})),
            budgets=BudgetsConfig(**data.get("budgets", {})),
            services=ServicesConfig(**data.get("services", {})),
            providers=ProvidersConfig(**data.get("providers", {})),
            context_reuse=ContextReuseConfig(**data.get("context_reuse", {})),
            secrets=secrets,
        )
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration in {settings_path}:\n{e}") from e

    if require:
        config.require(*require)
    return config
