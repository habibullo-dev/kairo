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
