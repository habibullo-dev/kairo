"""B1 context-policy for the Phase 13 research services (Task 9). Every one of the five is
``public_only`` — the engine's bundle check refuses to hand it any non-public provenance
(private memory/Gmail/Drive, project content, repo code). A purely public bundle passes. Keyless
— pure over check_context_policy. This is the 10B Checkpoint-D guarantee, re-pinned by name for
firecrawl / exa / jina_reader / searxng / openai_image."""

from __future__ import annotations

import pytest

from kira.orchestration import check_context_policy
from kira.orchestration.context import (
    ContextBundle,
    ContextItem,
    ContextPolicyError,
    Provenance,
)
from kira.services.catalog import SERVICE_CATALOG, ContextPolicy

_FIVE = ("firecrawl", "exa", "jina_reader", "searxng", "openai_image")


def _bundle(provenance: Provenance) -> ContextBundle:
    return ContextBundle(
        (ContextItem(kind="memory", ref="m1", provenance=provenance, text="a secret note"),)
    )


@pytest.mark.parametrize("name", _FIVE)
def test_each_service_is_public_only(name) -> None:
    assert SERVICE_CATALOG[name].context_policy is ContextPolicy.PUBLIC_ONLY


@pytest.mark.parametrize("name", _FIVE)
@pytest.mark.parametrize(
    "provenance",
    [Provenance.PRIVATE, Provenance.PROJECT_NON_PRIVATE, Provenance.REPO_CODE],
)
def test_non_public_bundle_is_refused(name, provenance) -> None:
    # Private, project, and repo-code provenance are ALL refused for a public_only service —
    # private content (and even a non-private project brief) can never reach these hosted tools.
    policy = SERVICE_CATALOG[name].context_policy
    with pytest.raises(ContextPolicyError):
        check_context_policy(_bundle(provenance), policy)


@pytest.mark.parametrize("name", _FIVE)
def test_public_bundle_passes(name) -> None:
    # A purely public bundle is the only thing a public_only service may receive.
    check_context_policy(_bundle(Provenance.PUBLIC), SERVICE_CATALOG[name].context_policy)


def test_mixed_bundle_with_one_private_item_is_refused() -> None:
    # Even one private item in an otherwise-public bundle taints the whole bundle for a
    # public_only service.
    bundle = ContextBundle(
        (
            ContextItem(kind="kb", ref="k1", provenance=Provenance.PUBLIC, text="public fact"),
            ContextItem(kind="memory", ref="m1", provenance=Provenance.PRIVATE, text="secret"),
        )
    )
    with pytest.raises(ContextPolicyError):
        check_context_policy(bundle, ContextPolicy.PUBLIC_ONLY)
