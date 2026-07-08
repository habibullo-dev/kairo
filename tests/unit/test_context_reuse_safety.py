"""Context-reuse safety non-negotiables (S7.6). Keyless, mostly structural.

Cache is NOT memory, and caching may never widen data-flow: no persistence/IO in the cache
layer, no prewarming, default = stable non-sensitive prefix only, and NO provider caches a
private prefix unless it is both permitted for private context AND the route allows it.
"""

from __future__ import annotations

import pathlib

from jarvis.models.context_reuse import capability, plan
from jarvis.models.prompt_layout import PromptSection, SectionKind, assemble
from jarvis.models.providers import PROVIDER_CATALOG

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "jarvis"
_CR = (_SRC / "models" / "context_reuse.py").read_text(encoding="utf-8")
_PL = (_SRC / "models" / "prompt_layout.py").read_text(encoding="utf-8")


def _prompt(sensitive: bool):
    return assemble(
        [
            PromptSection(SectionKind.SYSTEM_CONTRACT, "contract"),
            PromptSection(SectionKind.PROJECT_POLICY, "detail", sensitive=sensitive),
        ]
    )


def test_cache_layer_is_not_memory_no_io() -> None:
    # Cache is NOT memory: the policy/layout layer neither persists nor sends anything.
    for text in (_CR, _PL):
        for banned in ("aiosqlite", "sqlite3", "httpx", "requests", "open(", "os.environ", "Path("):
            assert banned not in text, f"context-reuse layer must not do I/O (found {banned!r})"


def test_no_prewarming_of_private_data() -> None:
    # No prewarm path exists — nothing pre-fills a cache (least of all with private data).
    assert "prewarm" not in (_CR + _PL).lower()


def test_default_provider_caches_nothing() -> None:
    # Fail-closed default: a provider with no explicit context-reuse resolves to OFF.
    from dataclasses import replace

    import jarvis.models.context_reuse as cr

    bare = replace(
        PROVIDER_CATALOG["deepseek"], supports_context_reuse=False, context_reuse_mode="off"
    )
    saved = cr.provider_spec
    cr.provider_spec = lambda name: bare
    try:
        assert not plan(cr.capability("x"), _prompt(False)).emit
    finally:
        cr.provider_spec = saved


def test_no_provider_caches_a_private_prefix_without_route_permission() -> None:
    # Across the WHOLE catalog: a sensitive stable prefix is never cached unless the route
    # explicitly allows it — and even then only where the provider permits private caching.
    for name in PROVIDER_CATALOG:
        cap = capability(name)
        assert not plan(cap, _prompt(sensitive=True), route_allows_private=False).emit, name
        with_route = plan(cap, _prompt(sensitive=True), route_allows_private=True)
        if with_route.emit:
            assert cap.cache_private_allowed, f"{name}: cached a private prefix but not private-ok"


def test_private_caching_capability_implies_private_ok() -> None:
    # cache_private_allowed can only ever be set on a provider that may receive private context.
    for name, spec in PROVIDER_CATALOG.items():
        if spec.cache_private_allowed:
            assert spec.private_ok, name


def test_non_sensitive_prefix_is_the_default_cacheable_content() -> None:
    # The default path: a non-sensitive stable prefix caches on a supporting provider with no
    # special permission needed.
    assert plan(capability("anthropic"), _prompt(sensitive=False)).emit
