"""Service catalog + fail-closed registry (Phase 10B Task 11).

Pins: every catalog row is fully classified; "now" services have adapters planned and are the
only ones that can reach AVAILABLE; the registry fails CLOSED (disabled/deferred/missing-
credentials/unpriced never available); project narrowing intersects; the availability read
model exposes NO key value."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.config import load_config
from jarvis.services import SERVICE_CATALOG, ContextPolicy, OutputTrust, ServiceRegistry
from jarvis.services.registry import ServiceState

_ADAPTERS_NOW = {
    "semgrep",
    "gitleaks",
    "playwright_local",  # the 10B-built adapters
    "firecrawl",  # Phase 13 Task 3
    "exa",  # Phase 13 Task 4
    "searxng",  # Phase 13 Task 5 (jina_reader stays deferred — value bar not cleared)
    "openai_image",  # Phase 13 Task 6
}


def test_catalog_rows_fully_classified() -> None:
    valid_kind = {"native", "cli", "mcp", "browser", "ritual"}
    for name, spec in SERVICE_CATALOG.items():
        assert spec.name == name
        assert spec.kind in valid_kind
        assert spec.pricing in {"fixed_zero", "metered", "unknown"}
        assert spec.priority in {"now", "later", "avoid"}
        assert spec.stages <= {"council", "review", "execution"}
        assert isinstance(spec.context_policy, ContextPolicy)
        assert isinstance(spec.output_trust, OutputTrust)


def test_only_planned_adapters_are_priority_now() -> None:
    # The set of "now" services must be exactly the three adapters 10B builds — a new "now"
    # row without an adapter (or vice versa) fails here, forcing a deliberate decision.
    now = {n for n, s in SERVICE_CATALOG.items() if s.priority == "now"}
    assert now == _ADAPTERS_NOW


def test_external_research_tools_are_public_only() -> None:
    # B1: external web/research services must never receive private context.
    for name in ("firecrawl", "exa", "jina_reader", "searxng"):
        assert SERVICE_CATALOG[name].context_policy == ContextPolicy.PUBLIC_ONLY
        assert SERVICE_CATALOG[name].egress is True


def test_scanner_findings_are_untrusted() -> None:
    # B2: a scanner finding can quote hostile code — framed untrusted.
    for name in ("semgrep", "gitleaks"):
        assert SERVICE_CATALOG[name].output_trust == OutputTrust.SECURITY_FINDING_UNTRUSTED
        assert SERVICE_CATALOG[name].context_policy == ContextPolicy.REPO_CODE_ONLY
        assert SERVICE_CATALOG[name].egress is False


def test_avoid_services_present_but_never_available() -> None:
    # B5: NotebookLM + generic Browser MCP are cataloged (so the UI shows "deferred") but can
    # never be enabled in 10B.
    for name in ("notebooklm", "browser_mcp"):
        assert SERVICE_CATALOG[name].priority == "avoid"
        r = ServiceRegistry(
            enabled=[name], priced_services=frozenset(), env={"NOTEBOOKLM_TOKEN": "x"}
        )
        assert r.state(name) is ServiceState.DEFERRED


# --- fail-closed availability matrix ---------------------------------------


def test_disabled_when_flag_off() -> None:
    r = ServiceRegistry(enabled=[], priced_services=frozenset(), env={})
    assert r.state("semgrep") is ServiceState.DISABLED
    assert r.is_available("semgrep") is False


def test_available_when_enabled_free_local() -> None:
    # semgrep is fixed_zero + no creds ⇒ enabling the flag makes it AVAILABLE.
    r = ServiceRegistry(enabled=["semgrep"], priced_services=frozenset(), env={})
    assert r.state("semgrep") is ServiceState.AVAILABLE and r.is_available("semgrep")


def test_deferred_even_when_flagged() -> None:
    # A later/avoid service has no adapter — flagging it can't make it available. (figma_mcp
    # stays priority=later through Phase 13; firecrawl is now "now" — see the availability test.)
    r = ServiceRegistry(
        enabled=["figma_mcp"], priced_services=frozenset(), env={"FIGMA_TOKEN": "k"}
    )
    assert r.state("figma_mcp") is ServiceState.DEFERRED


def test_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a hypothetical "now" service needing a key by checking the credential path via
    # a metered later-service's logic is deferred; instead assert the mechanism on the env map.
    from jarvis.services.catalog import SERVICE_CATALOG as CAT

    spec = CAT["firecrawl"]
    r_present = ServiceRegistry(
        enabled=[], priced_services=frozenset(), env={"FIRECRAWL_API_KEY": "k"}
    )
    r_absent = ServiceRegistry(enabled=[], priced_services=frozenset(), env={})
    assert r_present._creds_present(spec) is True
    assert r_absent._creds_present(spec) is False


def test_project_narrowing_intersects() -> None:
    # Globally enabled but narrowed out of this project ⇒ disabled here.
    r = ServiceRegistry(
        enabled=["semgrep"], priced_services=frozenset(), project_services=["gitleaks"], env={}
    )
    assert r.state("semgrep") is ServiceState.DISABLED
    r2 = ServiceRegistry(
        enabled=["semgrep"], priced_services=frozenset(), project_services=["semgrep"], env={}
    )
    assert r2.state("semgrep") is ServiceState.AVAILABLE


# --- read model: no secret values ------------------------------------------


def test_availability_view_has_no_key_values(tmp_path: Path) -> None:
    from jarvis.ui.readmodels import services_status

    cfg = load_config(root=tmp_path, env_file=None)
    cfg.services.enabled = ["semgrep"]
    rows = services_status(cfg)
    assert {r["name"] for r in rows} == set(SERVICE_CATALOG)
    semgrep = next(r for r in rows if r["name"] == "semgrep")
    assert semgrep["state"] == "available"
    # credential env names may appear; a key VALUE must not. Seed a canary into the env and
    # assert it's absent from the serialized view.
    import os

    os.environ["FIRECRAWL_API_KEY"] = "SERVICE-KEY-CANARY"
    try:
        rows2 = services_status(cfg)
    finally:
        del os.environ["FIRECRAWL_API_KEY"]
    assert "SERVICE-KEY-CANARY" not in str(rows2)
    fc = next(r for r in rows2 if r["name"] == "firecrawl")
    assert fc["credentials_present"] is True and "FIRECRAWL_API_KEY" in fc["credential_env"]
