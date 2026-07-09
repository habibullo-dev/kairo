"""Provider catalog + fail-closed availability (Phase 10C).

Mirrors the Phase 10B SERVICE_CATALOG / ServiceRegistry pattern (ADR-0015) for LLM providers:
the catalog is the safety model, enforcement is DERIVED from it, never hand-set per call site.

Every candidate provider is a :class:`ProviderSpec` row. A provider is AVAILABLE — its routes
may resolve, a role may run on it — only when it is (core OR flag-enabled in
``providers.enabled``) AND its credential env var is present AND it has ≥1 priced model in
``pricing.yaml``. Anything else is a specific non-available state the UI renders and the route
resolution rejects (fail-closed): a missing key / disabled provider / unpriced provider never
becomes usable and never silently downgrades to another provider.

Two authority axes are catalog-static (enforced in :func:`jarvis.models.registry.validate_route`,
which is pure — no config needed): ``trusted_authority`` (may hold final-authority roles) and
``private_ok`` (may receive PRIVATE-provenance context). ``trusted_authority`` is ``anthropic``
only (planner/judge/utility stay on Claude). ``private_ok`` was ``anthropic`` only in 10C;
Phase 15.6 widens it to ``{anthropic, gemini, openai}`` (Habib-approved) so the cost-aware Auto
router may route the private main chat to Gemini as well as Claude — the cheap workers
(qwen/deepseek/zai) remain ``private_ok=False`` and their privacy refusal is enforced in the
orchestration engine before fan-out (and, for the interactive router, by the same private_ok gate).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class ProviderSpec:
    """One provider's classification. ``api_style`` selects the client adapter:
    ``anthropic`` (the native streaming client), ``anthropic_compat`` (same client +
    ``base_url`` + a capability-degradation profile), or ``openai_compat`` (the text-only
    OpenAI chat client + ``base_url``). ``auth_style`` matters only for anthropic_compat:
    ``x-api-key`` (DeepSeek/Qwen) sends the key as the api-key header; ``bearer`` (Z.ai) sends
    it as ``Authorization: Bearer`` (the SDK ``auth_token`` param)."""

    name: str
    api_style: str  # "anthropic" | "anthropic_compat" | "openai_compat"
    key_service: str  # Config.require() name -> env var (also in config._REQUIRED_KEYS)
    credential_env: tuple[str, ...]
    tool_capable: bool  # may hold TOOL_CAPABLE_ROLES (a write-capable executor)
    private_ok: bool  # may receive PRIVATE-provenance context (anthropic only, 10C)
    trusted_authority: bool  # may hold FINAL_AUTHORITY_ROLES / PRIVATE_CONTEXT_ROLES
    core: bool  # always enabled (anthropic/openai) — NOT gated by providers.enabled
    default_base_url: str | None = None  # None for core providers (SDK default)
    auth_style: str = "x-api-key"  # anthropic_compat only: "x-api-key" | "bearer"
    default_models: tuple[str, ...] = ()  # documented model ids (routing pins the exact one)
    note: str = ""
    # --- Context reuse capability (S7): provider-agnostic prompt/context caching. Defaults are
    # fail-closed (off), so a new/unverified provider caches nothing until its behavior is
    # confirmed against live docs. See jarvis.models.context_reuse for the resolver + policy.
    supports_context_reuse: bool = False
    #: off | automatic_prefix | explicit_breakpoint | explicit_resource | provider_default
    context_reuse_mode: str = "off"
    supports_cache_key: bool = False  # provider honors a caller cache key (OpenAI prompt_cache_key)
    supports_cache_ttl: bool = False  # caller may choose a TTL (Anthropic 5m/1h)
    reports_cached_tokens: bool = False  # usage reports cached/hit tokens we can ledger
    cache_min_tokens: int = 0  # provider's minimum cacheable prefix (0 = unknown/none)
    cache_ttl_options: tuple[str, ...] = ()  # selectable TTLs, if any
    #: May PRIVATE/project content be cached here (only ever true when private_ok); even then a
    #: write is gated by route + audit. Default False ⇒ stable non-sensitive prefix only.
    cache_private_allowed: bool = False


#: The provider catalog. Endpoints/model ids verified against official provider docs
#: (2026-07-08); all base URLs are config-overridable via ``providers.base_urls``.
PROVIDER_CATALOG: dict[str, ProviderSpec] = {
    # --- core (always enabled; gated only by key presence + pricing) ---
    "anthropic": ProviderSpec(
        name="anthropic",
        api_style="anthropic",
        key_service="anthropic",
        credential_env=("ANTHROPIC_API_KEY",),
        tool_capable=True,
        private_ok=True,
        trusted_authority=True,
        core=True,
        note="head planner / architect / synthesizer / final reviewer / judge (Fable/Opus).",
        supports_context_reuse=True,
        context_reuse_mode="explicit_breakpoint",  # cache_control at the stable/volatile seam
        supports_cache_ttl=True,
        reports_cached_tokens=True,  # usage.cache_creation_input_tokens / cache_read_input_tokens
        cache_min_tokens=1024,
        cache_ttl_options=("5m", "1h"),
        cache_private_allowed=True,  # the only private_ok provider; still gated by route + audit
    ),
    "openai": ProviderSpec(
        name="openai",
        api_style="openai_compat",
        key_service="openai",
        credential_env=("OPENAI_API_KEY",),
        tool_capable=False,  # text-only adapter (Phase 10)
        private_ok=True,  # Phase 15.6: private-capable backup/utility (not default; not trusted)
        trusted_authority=False,
        core=True,
        default_models=("gpt-5.2", "gpt-5.2-mini"),
        note="text-only analysis; private-capable backup/utility (opt-in, never the default).",
        supports_context_reuse=True,
        context_reuse_mode="automatic_prefix",  # automatic prefix caching + prompt_cache_key
        supports_cache_key=True,
        reports_cached_tokens=True,  # usage.prompt_tokens_details.cached_tokens
        cache_min_tokens=1024,
    ),
    # --- Phase 10C opt-in workers (gated by providers.enabled) ---
    "deepseek": ProviderSpec(
        name="deepseek",
        api_style="anthropic_compat",
        key_service="deepseek",
        credential_env=("DEEPSEEK_API_KEY",),
        tool_capable=True,
        private_ok=False,
        trusted_authority=False,
        core=False,
        default_base_url="https://api.deepseek.com/anthropic",
        auth_style="x-api-key",
        default_models=("deepseek-v4-pro", "deepseek-v4-flash"),
        note="cheap coding / debugging / test-fix worker; strong price-performance.",
        supports_context_reuse=True,
        context_reuse_mode="automatic_prefix",  # automatic on-disk prefix caching (no control)
        reports_cached_tokens=True,  # usage prompt_cache_hit_tokens / prompt_cache_miss_tokens
    ),
    "qwen": ProviderSpec(
        name="qwen",
        api_style="anthropic_compat",
        key_service="qwen",
        credential_env=("DASHSCOPE_API_KEY",),
        tool_capable=True,
        private_ok=False,
        trusted_authority=False,
        core=False,
        # International DashScope Anthropic-compatible (Coding-Plan) endpoint; a pay-as-you-go
        # DashScope plan may use a different path — confirm + override at live verification.
        default_base_url="https://coding-intl.dashscope.aliyuncs.com/apps/anthropic",
        auth_style="x-api-key",
        default_models=("qwen3-coder-plus", "qwen3-coder-flash"),
        note="cheap coding / tests / boilerplate / extraction worker (NON-private only). "
        "Priced (DashScope International, 32K-128K band) in pricing.yaml.",
        supports_context_reuse=True,
        context_reuse_mode="explicit_breakpoint",  # DashScope cache_control blocks
        reports_cached_tokens=True,
    ),
    "zai": ProviderSpec(
        name="zai",
        api_style="anthropic_compat",
        key_service="zai",
        credential_env=("ZAI_API_KEY",),
        tool_capable=True,
        private_ok=False,
        trusted_authority=False,
        core=False,
        default_base_url="https://api.z.ai/api/anthropic",
        auth_style="bearer",  # Z.ai uses ANTHROPIC_AUTH_TOKEN (Authorization: Bearer)
        default_models=("glm-4.7", "glm-4-air", "glm-5.1"),
        note="GLM agentic coding / long-horizon sub-agent worker; senior-worker under review.",
        supports_context_reuse=False,  # OFF until Z.ai cache behavior is verified against docs
        context_reuse_mode="off",
    ),
    "gemini": ProviderSpec(
        name="gemini",
        api_style="openai_compat",
        key_service="gemini",
        credential_env=("GEMINI_API_KEY",),  # NOT GOOGLE_CLIENT_ID/SECRET (Phase 9 connectors)
        tool_capable=False,  # text-only this phase; tool/function calling deferred
        private_ok=True,  # Phase 15.6: private-capable (Auto router + daily worker); not trusted
        trusted_authority=False,
        core=False,
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_models=("gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash"),
        note="Phase 15.6 cost-aware routing: flash-lite = Auto router/classifier, flash = cheap "
        "daily worker; private-capable (private_ok) but not trusted_authority; text-only.",
        supports_context_reuse=True,
        # Implicit caching by default; explicit CachedContent resources DEFERRED (needs a privacy
        # review — see the S7 ADR) so private/large-doc caching is not enabled here.
        context_reuse_mode="provider_default",
        reports_cached_tokens=True,  # usage cachedContentTokenCount (when the model caches)
    ),
}


#: Providers permitted to hold FINAL_AUTHORITY_ROLES / PRIVATE_CONTEXT_ROLES (roles.py).
#: Derived from the catalog — the single source of truth. 10C: anthropic only.
TRUSTED_AUTHORITY_PROVIDERS: frozenset[str] = frozenset(
    name for name, spec in PROVIDER_CATALOG.items() if spec.trusted_authority
)


def provider_spec(name: str) -> ProviderSpec | None:
    return PROVIDER_CATALOG.get(name)


class ProviderState(StrEnum):
    AVAILABLE = "available"
    DISABLED = "disabled"  # opt-in provider, but not in providers.enabled
    MISSING_CREDENTIALS = "missing_credentials"  # enabled but the key env var is unset
    UNPRICED = "unpriced"  # enabled + key, but no priced model in pricing.yaml (fail-closed)
    UNKNOWN = "unknown"  # not in the catalog


class ProviderRegistry:
    """Resolves provider availability, fail-closed. ``enabled`` is the opt-in flag list
    (``config.providers.enabled``); ``priced_providers`` is the set of provider names with
    ≥1 priced model in the pricing table. Credential presence is checked by env-var
    *presence* only — a value never leaves this module."""

    def __init__(
        self,
        *,
        enabled: list[str] | None = None,
        priced_providers: frozenset[str] = frozenset(),
        env: dict[str, str] | None = None,
    ) -> None:
        self.enabled = set(enabled or [])
        self.priced_providers = priced_providers
        self._env = env if env is not None else os.environ

    @classmethod
    def from_config(cls, config: object, pricing: object | None = None) -> ProviderRegistry:
        """Build from a loaded Config (+ optional pre-loaded PricingTable). Credential presence
        is checked against the LOADED ``config.secrets`` (the same source the client factory and
        ``config.require`` use) — not raw ``os.environ`` — so a config loaded from a specific
        env_file is authoritative and matches what would actually be used to build a client."""
        from jarvis.config import _REQUIRED_KEYS
        from jarvis.observability.cost import load_pricing

        if pricing is None:
            pricing = load_pricing(config.root / "config" / "pricing.yaml")  # type: ignore[attr-defined]
        enabled = list(getattr(getattr(config, "providers", None), "enabled", []) or [])
        env = {var: getattr(config.secrets, attr, "") for attr, var in _REQUIRED_KEYS.values()}  # type: ignore[attr-defined]
        return cls(enabled=enabled, priced_providers=pricing.priced_providers(), env=env)  # type: ignore[attr-defined]

    def _creds_present(self, spec: ProviderSpec) -> bool:
        return all(bool(self._env.get(var)) for var in spec.credential_env)

    def state(self, name: str) -> ProviderState:
        spec = PROVIDER_CATALOG.get(name)
        if spec is None:
            return ProviderState.UNKNOWN
        if not spec.core and name not in self.enabled:
            return ProviderState.DISABLED
        if not self._creds_present(spec):
            return ProviderState.MISSING_CREDENTIALS
        if name not in self.priced_providers:
            return ProviderState.UNPRICED  # fail closed on absent pricing
        return ProviderState.AVAILABLE

    def is_available(self, name: str) -> bool:
        return self.state(name) is ProviderState.AVAILABLE

    def route_allowed(self, name: str) -> bool:
        """May a model route resolve to this provider? CORE providers (anthropic/openai) always
        may — their key is enforced fail-closed at the client factory (``config.require``), so a
        keyless route resolution stays valid and only the actual client build fails. An OPT-IN
        provider must be fully AVAILABLE (enabled ∧ key ∧ priced) to be a routing target."""
        spec = PROVIDER_CATALOG.get(name)
        if spec is None:
            return False
        return True if spec.core else self.is_available(name)

    def availability(self) -> list[dict]:
        """A presence-only view of every catalog provider for the Studio/Hub. NEVER a key
        value — only whether the required credential env var is set."""
        out: list[dict] = []
        for name, spec in sorted(PROVIDER_CATALOG.items()):
            out.append(
                {
                    "name": name,
                    "api_style": spec.api_style,
                    "tool_capable": spec.tool_capable,
                    "trusted_authority": spec.trusted_authority,
                    "private_ok": spec.private_ok,
                    "core": spec.core,
                    "default_models": list(spec.default_models),
                    "state": self.state(name).value,
                    "credentials_present": self._creds_present(spec),
                    "credential_env": list(spec.credential_env),  # names only, never values
                    "note": spec.note,
                }
            )
        return out
