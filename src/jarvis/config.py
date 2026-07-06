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
_REQUIRED_KEYS: dict[str, tuple[str, str]] = {
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "voyage": ("voyage_api_key", "VOYAGE_API_KEY"),
    "tavily": ("tavily_api_key", "TAVILY_API_KEY"),
    "openai": ("openai_api_key", "OPENAI_API_KEY"),  # cloud STT (Phase 7, opt-in)
    "elevenlabs": ("elevenlabs_api_key", "ELEVENLABS_API_KEY"),  # cloud TTS (Phase 7, opt-in)
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


class ModelsConfig(BaseModel):
    """Model IDs per role. Quality-first — see config/settings.yaml."""

    main: str = "claude-opus-4-8"
    utility: str = "claude-sonnet-5"
    judge: str = "claude-opus-4-8"
    embedding: str = "voyage-3-large"


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


class PathsConfig(BaseModel):
    """Filesystem locations, relative to the project root unless absolute."""

    data_dir: Path = Path("data")
    logs_dir: Path = Path("logs")


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
            paths=PathsConfig(**data.get("paths", {})),
            secrets=secrets,
        )
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration in {settings_path}:\n{e}") from e

    if require:
        config.require(*require)
    return config
