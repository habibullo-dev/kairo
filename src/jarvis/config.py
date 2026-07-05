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
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# service name -> (Secrets attribute, env var name) for require() error messages.
_REQUIRED_KEYS: dict[str, tuple[str, str]] = {
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "voyage": ("voyage_api_key", "VOYAGE_API_KEY"),
    "tavily": ("tavily_api_key", "TAVILY_API_KEY"),
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


class PathsConfig(BaseModel):
    """Filesystem locations, relative to the project root unless absolute."""

    data_dir: Path = Path("data")
    logs_dir: Path = Path("logs")


class Config(BaseModel):
    """Fully assembled configuration passed through the app."""

    root: Path
    models: ModelsConfig
    limits: LimitsConfig
    # Phase 2; default_factory keeps direct Config(...) callers simple.
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
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
            paths=PathsConfig(**data.get("paths", {})),
            secrets=secrets,
        )
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration in {settings_path}:\n{e}") from e

    if require:
        config.require(*require)
    return config
