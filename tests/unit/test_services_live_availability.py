"""Fail-closed availability matrix for the Phase 13 research services (Task 9). The consolidated
pin over the five services × states: only ALL-GOOD registers; flag-off / key-missing / unpriced /
narrowed-out / deferred each resolve to their specific non-available state and the tool never
becomes usable. Keyless — pure over the ServiceRegistry."""

from __future__ import annotations

import pytest

from kira.services import ServiceRegistry
from kira.services.registry import ServiceState

# The four shipped "now" services: (credential env var or None, metered?)
_NOW: dict[str, tuple[str | None, bool]] = {
    "firecrawl": ("FIRECRAWL_API_KEY", True),
    "exa": ("EXA_API_KEY", True),
    "searxng": (None, False),  # no key, fixed_zero
    "openai_image": ("OPENAI_API_KEY", True),
}


def _reg(name, *, enabled=True, key=True, priced=True, project=None) -> ServiceRegistry:
    cred, metered = _NOW[name]
    env = {cred: "k"} if (key and cred) else {}
    priced_services = frozenset({name}) if (priced and metered) else frozenset()
    return ServiceRegistry(
        enabled=[name] if enabled else [],
        priced_services=priced_services,
        project_services=project,
        env=env,
    )


@pytest.mark.parametrize("name", list(_NOW))
def test_all_good_is_the_only_available_state(name) -> None:
    assert _reg(name).state(name) is ServiceState.AVAILABLE
    assert _reg(name).is_available(name)


@pytest.mark.parametrize("name", list(_NOW))
def test_flag_off_is_disabled(name) -> None:
    r = _reg(name, enabled=False)
    assert r.state(name) is ServiceState.DISABLED and not r.is_available(name)


@pytest.mark.parametrize("name", list(_NOW))
def test_narrowed_out_of_project_is_disabled(name) -> None:
    # Enabled globally, but the project narrows to a set that excludes it ⇒ DISABLED here.
    r = _reg(name, project=["some_other_service"])
    assert r.state(name) is ServiceState.DISABLED and not r.is_available(name)


def test_missing_key_only_bites_keyed_services() -> None:
    for name in ("firecrawl", "exa", "openai_image"):
        r = _reg(name, key=False)
        assert r.state(name) is ServiceState.MISSING_CREDENTIALS and not r.is_available(name)
    # searxng needs no key ⇒ absence of a key can never make it missing_credentials.
    assert _reg("searxng", key=False).state("searxng") is ServiceState.AVAILABLE


def test_unpriced_only_bites_metered_services() -> None:
    for name in ("firecrawl", "exa", "openai_image"):
        r = _reg(name, priced=False)
        assert r.state(name) is ServiceState.UNPRICED and not r.is_available(name)
    # searxng is fixed_zero ⇒ it needs no pricing row and can never be unpriced.
    assert _reg("searxng", priced=False).state("searxng") is ServiceState.AVAILABLE


def test_jina_reader_is_deferred_even_fully_configured() -> None:
    # Jina did not clear the Task-5 value bar (priority stays 'later') — no adapter, so flagging
    # it + a key + pricing can never make it AVAILABLE.
    r = ServiceRegistry(
        enabled=["jina_reader"],
        priced_services=frozenset({"jina_reader"}),
        env={"JINA_API_KEY": "k"},
    )
    assert r.state("jina_reader") is ServiceState.DEFERRED and not r.is_available("jina_reader")


def test_matrix_only_all_good_registers() -> None:
    # One assertion over the whole matrix: for each service, exactly the all-good cell is usable.
    for name in _NOW:
        states = {
            "all_good": _reg(name).is_available(name),
            "flag_off": _reg(name, enabled=False).is_available(name),
            "narrowed": _reg(name, project=["x"]).is_available(name),
        }
        assert states == {"all_good": True, "flag_off": False, "narrowed": False}, name
