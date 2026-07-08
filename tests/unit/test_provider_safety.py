"""Provider authority + privacy + fail-closed routing pins (Phase 10C, T2).

The load-bearing 10C safety guarantees, adversarially pinned:
* planner / judge / utility can ONLY resolve to a trusted provider (anthropic), at EVERY
  override layer — no settings/project/run route can hand final authority (or private
  conversation content) to a cheap worker;
* a route to a disabled / missing-key / unpriced provider fails CLOSED (RouteError), never a
  silent downgrade; core providers stay routable (key enforced at the factory);
* the orchestration engine REFUSES a PRIVATE-provenance bundle bound for a private_ok=False
  provider before any fan-out (a model is a context sink).
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.models.providers import ProviderRegistry
from jarvis.models.registry import ModelRegistry, RouteError
from jarvis.orchestration import ContextBundle, OrchestrationEngine, resolve_team
from jarvis.orchestration.context import ContextItem, Provenance
from jarvis.orchestration.engine import ProviderContextError

# --- authority pins ---------------------------------------------------------


@pytest.mark.parametrize("role", ["planner", "judge", "utility"])
@pytest.mark.parametrize("layer", ["settings", "project", "run"])
def test_authority_roles_reject_cheap_provider_at_every_layer(role: str, layer: str) -> None:
    override = {role: {"provider": "deepseek", "model": "deepseek-v4-flash"}}
    reg = ModelRegistry(override if layer == "settings" else None)
    kwargs: dict = {}
    if layer == "project":
        kwargs["project_routes"] = override
    elif layer == "run":
        kwargs["run_routes"] = override
    with pytest.raises(RouteError):
        reg.route(role, **kwargs)


@pytest.mark.parametrize("provider", ["deepseek", "qwen", "zai", "gemini", "openai"])
def test_no_nontrusted_provider_can_hold_final_authority(provider: str) -> None:
    reg = ModelRegistry({"planner": {"provider": provider, "model": "m"}})
    with pytest.raises(RouteError):
        reg.route("planner")


def test_default_authority_routes_resolve_to_anthropic() -> None:
    reg = ModelRegistry()
    for role in ("planner", "judge", "utility"):
        assert reg.route(role).provider == "anthropic"


def test_worker_role_may_route_to_cheap_provider_on_authority_grounds() -> None:
    # researcher is a worker role: no authority pin (availability is a separate gate below).
    reg = ModelRegistry({"researcher": {"provider": "deepseek", "model": "deepseek-v4-flash"}})
    assert reg.route("researcher").provider == "deepseek"


def test_tool_capable_role_rejects_text_only_provider() -> None:
    # coder must drive tools; Gemini is text-only (tool_capable=False) ⇒ rejected at validation,
    # not left to fail at the client. text_only is NOT set on the route — the provider is.
    reg = ModelRegistry({"coder": {"provider": "gemini", "model": "gemini-2.5-flash"}})
    with pytest.raises(RouteError, match="must drive tools"):
        reg.route("coder")


def test_text_only_provider_allowed_on_worker_analysis_role() -> None:
    # Gemini's intended use: an analysis/research worker that doesn't drive tools.
    reg = ModelRegistry({"researcher": {"provider": "gemini", "model": "gemini-2.5-flash"}})
    assert reg.route("researcher").provider == "gemini"


# --- fail-closed availability -----------------------------------------------


def _preg(enabled: list[str], priced: list[str], env: dict[str, str]) -> ProviderRegistry:
    return ProviderRegistry(enabled=enabled, priced_providers=frozenset(priced), env=env)


def _worker_reg(provider: str, model: str, preg: ProviderRegistry) -> ModelRegistry:
    routes = {"researcher": {"provider": provider, "model": model}}
    return ModelRegistry(routes, provider_registry=preg)


def test_route_to_disabled_provider_fails_closed() -> None:
    preg = _preg([], ["deepseek"], {"DEEPSEEK_API_KEY": "k"})  # key + pricing, but not enabled
    with pytest.raises(RouteError):
        _worker_reg("deepseek", "deepseek-v4-flash", preg).route("researcher")


def test_route_to_missing_key_provider_fails_closed() -> None:
    preg = _preg(["deepseek"], ["deepseek"], {})  # enabled + priced, but no key
    with pytest.raises(RouteError):
        _worker_reg("deepseek", "deepseek-v4-flash", preg).route("researcher")


def test_route_to_unpriced_provider_fails_closed() -> None:
    preg = _preg(["qwen"], [], {"DASHSCOPE_API_KEY": "k"})  # enabled + key, but unpriced
    with pytest.raises(RouteError):
        _worker_reg("qwen", "qwen3-coder-plus", preg).route("researcher")


def test_available_worker_provider_resolves() -> None:
    preg = _preg(["deepseek"], ["deepseek"], {"DEEPSEEK_API_KEY": "k"})
    reg = _worker_reg("deepseek", "deepseek-v4-flash", preg)
    assert reg.route("researcher").provider == "deepseek"


def test_core_anthropic_route_allowed_without_enabled_or_key() -> None:
    # anthropic is core: routable even when absent from providers.enabled and keyless (its key
    # is enforced fail-closed at the client factory, not at route resolution).
    reg = ModelRegistry(provider_registry=_preg([], ["anthropic"], {}))
    assert reg.route("planner").provider == "anthropic"


# --- engine private-context refusal -----------------------------------------


async def _noop_spawn(**_: object) -> object:  # must never be called by the guard
    raise AssertionError("spawn must not run — the guard refuses before fan-out")


def _engine(registry: ModelRegistry) -> OrchestrationEngine:
    return OrchestrationEngine(
        spawn=_noop_spawn,
        store=object(),
        head_client=object(),
        head_model="m",
        turn_lock=asyncio.Lock(),
        registry=registry,
    )


_PRIVATE = ContextBundle(
    items=(ContextItem(kind="memory", ref="m1", provenance=Provenance.PRIVATE, text="secret"),)
)
_REPO = ContextBundle(
    items=(ContextItem(kind="repo_file", ref="a.py", provenance=Provenance.REPO_CODE, text="x"),)
)


def test_engine_refuses_private_bundle_to_cheap_provider() -> None:
    reg = ModelRegistry({"researcher": {"provider": "deepseek", "model": "deepseek-v4-flash"}})
    with pytest.raises(ProviderContextError):
        _engine(reg).check_provider_context(resolve_team("research"), _PRIVATE)


def test_engine_allows_private_bundle_to_all_anthropic_team() -> None:
    _engine(ModelRegistry()).check_provider_context(resolve_team("research"), _PRIVATE)  # no raise


def test_engine_allows_cheap_provider_for_nonprivate_bundle() -> None:
    reg = ModelRegistry({"researcher": {"provider": "deepseek", "model": "deepseek-v4-flash"}})
    _engine(reg).check_provider_context(resolve_team("research"), _REPO)  # no PRIVATE ⇒ no raise


# --- T7: consolidated adversarial routing matrix ----------------------------


@pytest.mark.parametrize("provider", ["deepseek", "qwen", "zai", "gemini"])
@pytest.mark.parametrize("role", ["planner", "judge", "utility"])
@pytest.mark.parametrize("layer", ["settings", "project", "run"])
def test_every_worker_provider_rejected_on_every_authority_role_and_layer(
    provider: str, role: str, layer: str
) -> None:
    override = {role: {"provider": provider, "model": "m"}}
    reg = ModelRegistry(override if layer == "settings" else None)
    kwargs: dict = {}
    if layer == "project":
        kwargs["project_routes"] = override
    elif layer == "run":
        kwargs["run_routes"] = override
    with pytest.raises(RouteError):
        reg.route(role, **kwargs)


def test_head_synthesizer_stays_anthropic_with_cheap_workers() -> None:
    # Even with cheap providers configured on worker roles, the head (planner) + judge — the
    # synthesis/verdict/eval-judge authority — stay anthropic. There is no override that moves it.
    reg = ModelRegistry(
        {
            "coder": {"provider": "deepseek", "model": "deepseek-v4-pro"},
            "researcher": {"provider": "gemini", "model": "gemini-2.5-flash"},
            "reviewer": {"provider": "zai", "model": "glm-4.7"},
        }
    )
    assert reg.route("planner").provider == "anthropic"
    assert reg.route("judge").provider == "anthropic"


def test_opt_in_providers_disabled_by_default_even_with_keys_and_pricing() -> None:
    # providers.enabled=[]: the four workers are NOT routable even with keys + pricing present;
    # the two core providers stay routable. This is the byte-identical / fail-closed default.
    allkeys = {v: "k" for v in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ZAI_API_KEY",
                                "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
    preg = _preg([], ["deepseek", "qwen", "zai", "gemini", "anthropic", "openai"], allkeys)
    for p in ("deepseek", "qwen", "zai", "gemini"):
        assert not preg.route_allowed(p)
    for p in ("anthropic", "openai"):
        assert preg.route_allowed(p)


def test_availability_gate_is_independent_of_authority() -> None:
    # A worker role clears the authority pin but a disabled provider still fails closed — the
    # two gates compose (authority is not a bypass of availability).
    reg = _worker_reg("gemini", "gemini-2.5-flash", _preg([], ["gemini"], {"GEMINI_API_KEY": "k"}))
    with pytest.raises(RouteError):
        reg.route("researcher")


# --- T7: Z.ai fail-closed proof (live console unavailable — kept in catalog) -------


def test_zai_missing_key_fails_closed() -> None:
    # Z.ai is enabled + priced but its key is absent (console unavailable). A worker route to it
    # fails closed at resolution, and the reason names the missing-credentials state.
    reg = _worker_reg("zai", "glm-4.7", _preg(["zai"], ["zai"], {}))
    with pytest.raises(RouteError, match="missing_credentials"):
        reg.route("researcher")


def test_zai_disabled_fails_closed() -> None:
    reg = _worker_reg("zai", "glm-4.7", _preg([], ["zai"], {"ZAI_API_KEY": "k"}))
    with pytest.raises(RouteError, match="disabled"):
        reg.route("researcher")


def test_zai_state_matrix() -> None:
    # The presence-only state the Studio renders for Z.ai across the fail-closed conditions.
    assert _preg([], ["zai"], {"ZAI_API_KEY": "k"}).state("zai").value == "disabled"
    assert _preg(["zai"], ["zai"], {}).state("zai").value == "missing_credentials"
    assert _preg(["zai"], [], {"ZAI_API_KEY": "k"}).state("zai").value == "unpriced"
    assert _preg(["zai"], ["zai"], {"ZAI_API_KEY": "k"}).state("zai").value == "available"


def test_zai_stays_in_catalog_and_is_authority_pinned() -> None:
    from jarvis.models.providers import PROVIDER_CATALOG

    assert "zai" in PROVIDER_CATALOG  # kept in the catalog despite no live access
    assert not PROVIDER_CATALOG["zai"].trusted_authority  # worker only
    assert not PROVIDER_CATALOG["zai"].private_ok
