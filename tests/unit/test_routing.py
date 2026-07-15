"""Cost-aware Auto routing (Phase 15.6) — policy + classifier + router, all keyless.

Safety focus: the classifier can only ESCALATE (never downgrade); every Auto tier is private_ok;
an unavailable tier or a classifier failure falls back to the trusted SAFE default (Sonnet), never
to a cheap/non-private provider.
"""

from __future__ import annotations

import pytest

from kira.core import FakeClient, text_message
from kira.models.providers import provider_spec
from kira.routing import ALL_TIERS, RoutingMode
from kira.routing.classifier import Classifier, _extract_json
from kira.routing.policy import (
    DEEP,
    JUDGMENT,
    PLANNING,
    SIMPLE,
    SIMPLE_TOOLED,
    Classification,
    choose_tier,
    coerce_classification,
    provider_for_model,
    resolve_route,
)
from kira.routing.router import Router, RoutingState

_ALL = lambda _p: True  # noqa: E731 - everything available
_NO_GEMINI = lambda p: p != "gemini"  # noqa: E731


def _c(**kw) -> Classification:
    base = dict(
        intent="i", difficulty="simple", sensitivity="non_sensitive", category="chat",
        needs_tools=True,
    )
    base.update(kw)
    return Classification(**base)


# --- policy: every Auto tier is private_ok (the load-bearing safety invariant) ---------
def test_all_auto_tiers_are_private_ok() -> None:
    for tier in ALL_TIERS:
        spec = provider_spec(tier.provider)
        assert spec is not None and spec.private_ok, f"{tier.key} → {tier.provider} not private_ok"


# --- policy: tier selection --------------------------------------------------
def test_simple_toolfree_goes_to_gemini_but_tool_need_goes_to_haiku() -> None:
    # Gemini is text-only: it serves ONLY tool-free simple turns; tool-needing simple work → Haiku.
    tool_free = _c(sensitivity="non_sensitive", category="chat", needs_tools=False)
    assert choose_tier(tool_free) is SIMPLE
    needs_tool = _c(sensitivity="non_sensitive", category="chat", needs_tools=True)
    assert choose_tier(needs_tool) is SIMPLE_TOOLED


@pytest.mark.parametrize(
    "kw",
    [
        {"sensitivity": "private"},
        {"sensitivity": "personal"},
        {"category": "email"},
        {"category": "calendar"},
        {"category": "finance"},
        {"difficulty": "hard"},
        {"category": "coding"},
    ],
)
def test_judgment_tier_for_sensitive_or_hard(kw) -> None:
    assert choose_tier(_c(**kw)) is JUDGMENT


def test_expert_goes_deep_and_planning_goes_fable() -> None:
    assert choose_tier(_c(difficulty="expert", category="other")) is DEEP
    assert choose_tier(_c(category="planning", difficulty="hard")) is PLANNING
    assert choose_tier(_c(category="planning", difficulty="expert")) is PLANNING


def test_opus_fable_are_not_the_simple_default() -> None:
    # Normal chat must NOT reach Opus/Fable — only expert/planning do. It lands on a cheap tier.
    t = choose_tier(_c(difficulty="moderate", sensitivity="non_sensitive", category="chat"))
    assert t in (SIMPLE, SIMPLE_TOOLED)


# --- policy: private_ok + availability gate ----------------------------------
def test_gemini_unavailable_downgrades_simple_to_haiku() -> None:
    # A tool-free simple turn wants Gemini; if Gemini is unavailable it drops to the cheap
    # tool-capable Haiku (NOT the pricier Sonnet) — still private_ok + always available.
    c = _c(difficulty="simple", sensitivity="non_sensitive", needs_tools=False)
    d = resolve_route(c, is_available=_NO_GEMINI)
    assert (d.provider, d.model) == ("anthropic", "claude-haiku-4-5-20251001")
    assert d.tools_enabled and "downgraded" in d.reason


def test_available_toolfree_simple_stays_on_gemini_flash_with_tools_off() -> None:
    c = _c(difficulty="simple", sensitivity="non_sensitive", needs_tools=False)
    d = resolve_route(c, is_available=_ALL)
    assert (d.provider, d.model) == ("gemini", "gemini-2.5-flash")
    assert d.mode == "auto" and d.effort is None and d.tools_enabled is False


def test_private_content_never_lands_on_a_non_private_provider() -> None:
    # Whatever the classification, the resolved provider is always private_ok.
    for kw in ({"sensitivity": "private"}, {"category": "finance"}, {"difficulty": "expert"}):
        d = resolve_route(_c(**kw), is_available=_ALL)
        assert provider_spec(d.provider).private_ok


# --- policy: coercion is fail-safe -------------------------------------------
def test_coerce_unknown_values_to_safe_extreme() -> None:
    c = coerce_classification({"difficulty": "banana", "sensitivity": "meh", "category": "xyz"})
    assert c.difficulty == "hard" and c.sensitivity == "private" and c.category == "other"
    assert c.needs_tools is True  # unknown ⇒ assume tools needed (route to a tool-capable model)


def test_coerce_empty_is_private_hard_needs_tools() -> None:
    c = coerce_classification({})
    assert c.sensitivity == "private" and c.difficulty == "hard" and c.needs_tools is True


def test_coerce_needs_tools_only_false_when_explicitly_false() -> None:
    assert coerce_classification({"needs_tools": False}).needs_tools is False
    assert coerce_classification({"needs_tools": "no"}).needs_tools is True  # non-bool ⇒ safe True


def test_provider_for_model_prefixes() -> None:
    assert provider_for_model("claude-sonnet-5") == "anthropic"
    assert provider_for_model("gemini-2.5-flash") == "gemini"
    assert provider_for_model("gpt-5.2") == "openai"
    assert provider_for_model("qwen3-coder-plus") == "qwen"
    assert provider_for_model("glm-5.1") == "zai"
    assert provider_for_model("mystery") == "anthropic"  # safe default


# --- classifier: JSON extraction + fail-safe ---------------------------------
def test_extract_json_tolerates_fence_and_prose() -> None:
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('here you go: {"a": {"b": 2}} trailing') == {"a": {"b": 2}}
    assert _extract_json("no json here") is None
    assert _extract_json("") is None


async def test_classifier_parses_valid_json() -> None:
    payload = '{"difficulty":"simple","sensitivity":"non_sensitive","category":"summary"}'
    clf = Classifier(FakeClient([text_message(payload)]), "gemini-2.5-flash-lite")
    c = await clf.classify("summarize this")
    assert c is not None and c.category == "summary" and c.sensitivity == "non_sensitive"


async def test_classifier_unparseable_returns_none() -> None:
    clf = Classifier(FakeClient([text_message("I cannot comply")]), "gemini-2.5-flash-lite")
    assert await clf.classify("hi") is None


async def test_classifier_error_returns_none() -> None:
    clf = Classifier(FakeClient([]), "gemini-2.5-flash-lite")  # empty ⇒ create() raises
    assert await clf.classify("hi") is None


# --- router: MANUAL vs AUTO --------------------------------------------------
async def test_manual_returns_pinned_model_and_effort() -> None:
    state = RoutingState(RoutingMode.MANUAL)
    r = Router(
        state=state,
        manual_model=lambda: "claude-opus-4-8",
        manual_effort=lambda: "low",
        classifier=None,
        is_available=_ALL,
    )
    d = await r.route("anything")
    assert d.mode == "manual" and d.model == "claude-opus-4-8" and d.effort == "low"
    assert d.provider == "anthropic"


async def test_auto_routes_simple_to_gemini() -> None:
    payload = '{"difficulty":"simple","sensitivity":"non_sensitive","needs_tools":false}'
    state = RoutingState(RoutingMode.AUTO)
    r = Router(
        state=state,
        manual_model=lambda: "claude-sonnet-5",
        manual_effort=lambda: None,
        classifier=Classifier(FakeClient([text_message(payload)]), "gemini-2.5-flash-lite"),
        is_available=_ALL,
    )
    d = await r.route("what's 2+2")
    assert d.mode == "auto" and (d.provider, d.model) == ("gemini", "gemini-2.5-flash")


async def test_auto_escalates_private_to_sonnet() -> None:
    payload = '{"difficulty":"moderate","sensitivity":"private","category":"email"}'
    r = Router(
        state=RoutingState(RoutingMode.AUTO),
        manual_model=lambda: "claude-sonnet-5",
        manual_effort=lambda: None,
        classifier=Classifier(FakeClient([text_message(payload)]), "gemini-2.5-flash-lite"),
        is_available=_ALL,
    )
    d = await r.route("draft a reply to my boss")
    assert (d.provider, d.model) == ("anthropic", "claude-sonnet-5") and d.sensitivity == "private"


async def test_auto_failsafe_on_bad_classification_escalates() -> None:
    # Unparseable classifier output ⇒ FAILSAFE (private/hard) ⇒ trusted Sonnet, never a cheap route.
    r = Router(
        state=RoutingState(RoutingMode.AUTO),
        manual_model=lambda: "claude-sonnet-5",
        manual_effort=lambda: None,
        classifier=Classifier(FakeClient([text_message("nope")]), "gemini-2.5-flash-lite"),
        is_available=_ALL,
    )
    d = await r.route("hello")
    assert (d.provider, d.model) == ("anthropic", "claude-sonnet-5")


async def test_auto_router_unavailable_falls_back_to_safe_default() -> None:
    r = Router(
        state=RoutingState(RoutingMode.AUTO),
        manual_model=lambda: "claude-sonnet-5",
        manual_effort=lambda: None,
        classifier=None,  # gemini not wired
        is_available=_NO_GEMINI,
    )
    d = await r.route("hello")
    assert (d.provider, d.model) == ("anthropic", "claude-sonnet-5") and "unavailable" in d.reason
