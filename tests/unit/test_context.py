"""ContextManager tests.

The centerpiece is the **compacted-view validity property test**: whatever cut or
elision the manager chooses, the view it produces must be a legal Anthropic message
list. Written before the loop is wired (a Phase 2 non-negotiable) — this is the net
that catches every future compaction regression.
"""

from __future__ import annotations

import pytest

from kira.core import FakeClient, text_message
from kira.core.context import CompactionView, ContextManager, _has_tool_result
from kira.observability.cost import Usage

# --- conversation builders -------------------------------------------------


def _turn(t: int, result_size: int = 200) -> list[dict]:
    """A full turn: user question -> assistant (thinking + tool_use) -> tool_result -> answer."""
    return [
        {"role": "user", "content": f"question {t} " + "x" * 40},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": f"reasoning {t}", "signature": f"sig{t}"},
                {
                    "type": "tool_use",
                    "id": f"t{t}",
                    "name": "read_file",
                    "input": {"path": f"f{t}"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"t{t}",
                    "content": "y" * result_size,
                    "is_error": False,
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": f"answer {t}"}]},
    ]


def _conversation(n_turns: int) -> list[dict]:
    msgs: list[dict] = []
    for t in range(n_turns):
        msgs.extend(_turn(t))
    return msgs


def _one_giant_turn(n_iters: int, result_size: int) -> list[dict]:
    """One user turn that never ends: many tool iterations, no second user boundary."""
    msgs: list[dict] = [{"role": "user", "content": "do a big multi-step task"}]
    for i in range(n_iters):
        msgs.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"t{i}",
                        "name": "read_file",
                        "input": {"path": f"f{i}"},
                    }
                ],
            }
        )
        msgs.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"t{i}",
                        "content": "z" * result_size,
                        "is_error": False,
                    }
                ],
            }
        )
    return msgs


# --- the validity invariant ------------------------------------------------


def _assert_only_tool_results_elided(a: dict, b: dict) -> None:
    """`a` (view) may differ from `b` (full) ONLY by elided tool_result bodies."""
    assert a["role"] == b["role"]
    assert isinstance(a["content"], list) and isinstance(b["content"], list)
    assert len(a["content"]) == len(b["content"])
    for ba, bb in zip(a["content"], b["content"], strict=True):
        if ba == bb:
            continue
        assert ba.get("type") == "tool_result" and bb.get("type") == "tool_result"
        assert ba["tool_use_id"] == bb["tool_use_id"]  # pairing id preserved
        assert ba.get("is_error") == bb.get("is_error")
        assert ba["content"].startswith("[elided:")  # only the body changed


def _assert_valid_view(view: CompactionView, full: list[dict]) -> None:
    vm = view.messages
    assert vm, "view must be non-empty"
    # (i) starts at a real user turn (no orphaned tool_result, no leading assistant)
    assert vm[0]["role"] == "user"
    assert not _has_tool_result(vm[0]["content"])
    # (ii) every tool_use id is answered by exactly one tool_result in the next message
    for i, m in enumerate(vm):
        if m["role"] == "assistant" and isinstance(m["content"], list):
            use_ids = [b["id"] for b in m["content"] if b.get("type") == "tool_use"]
            if use_ids:
                assert i + 1 < len(vm), "a tool_use must be followed by its result message"
                nxt = vm[i + 1]["content"]
                result_ids = [
                    b["tool_use_id"]
                    for b in nxt
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                assert sorted(use_ids) == sorted(result_ids)
    # (iii) byte-identical to full[cut:], except elided tool_result bodies
    basis = full[view.cut :]
    assert len(vm) == len(basis)
    for a, b in zip(vm, basis, strict=True):
        if a != b:
            _assert_only_tool_results_elided(a, b)


@pytest.mark.parametrize("n_turns", [1, 4, 8, 16, 32])
@pytest.mark.parametrize("budget", [1500, 4000, 12000])
def test_compacted_view_is_always_valid(n_turns: int, budget: int) -> None:
    full = _conversation(n_turns)
    cm = ContextManager(context_token_budget=budget, compaction_threshold=0.7, keep_fraction=0.5)
    _assert_valid_view(cm.view(full), full)


# --- specific behaviors ----------------------------------------------------


def test_small_conversation_is_not_compacted() -> None:
    full = _conversation(2)
    view = ContextManager().view(full)  # default 180k budget
    assert view.cut == 0 and view.elided == 0 and view.overflow is False
    assert view.messages is full  # untouched, same object


def test_large_conversation_actually_compacts_at_a_user_boundary() -> None:
    full = _conversation(40)
    cm = ContextManager(context_token_budget=3000, compaction_threshold=0.7, keep_fraction=0.5)
    view = cm.view(full)
    assert view.cut > 0  # a prefix was genuinely dropped
    assert len(view.messages) < len(full)
    assert full[view.cut]["role"] == "user"  # cut lands on a real user turn
    _assert_valid_view(view, full)


def test_mid_turn_overflow_elides_oldest_tool_results() -> None:
    full = _one_giant_turn(n_iters=20, result_size=2000)  # ~10k tokens, one boundary
    cm = ContextManager(context_token_budget=3000, compaction_threshold=0.7, keep_fraction=0.5)
    view = cm.view(full)
    assert view.cut == 0  # no second boundary to cut at
    assert view.elided > 0  # so it shrank tool_result bodies instead
    assert view.overflow is False  # eliding freed enough
    _assert_valid_view(view, full)


def test_overflow_when_even_elision_cannot_fit() -> None:
    # A giant tool_use *input* (assistant content) can't be elided — only results can.
    msgs = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t0",
                    "name": "write_file",
                    "input": {"content": "q" * 40000},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t0", "content": "ok", "is_error": False}
            ],
        },
    ]
    cm = ContextManager(context_token_budget=2000, compaction_threshold=0.7, keep_fraction=0.5)
    view = cm.view(msgs)
    assert view.overflow is True
    _assert_valid_view(view, msgs)  # still structurally valid, even though it won't be sent


# --- token accounting ------------------------------------------------------


def test_estimates_before_any_observe_for_resume() -> None:
    # A resumed 150k-char history must be seen as over-budget before the first call.
    full = _conversation(40)
    cm = ContextManager(context_token_budget=3000, compaction_threshold=0.7)
    assert cm.should_compact(full) is True  # no observe() needed


def test_observed_usage_floors_the_estimate() -> None:
    cm = ContextManager(context_token_budget=1000, compaction_threshold=0.7)
    tiny = [{"role": "user", "content": "hi"}]
    assert cm.should_compact(tiny) is False
    # the real tokenizer reported a large context last call — trust it as a floor
    cm.observe(Usage(input_tokens=600, cache_read_input_tokens=300))
    assert cm.should_compact(tiny) is True  # 900 > 0.7 * 1000


# --- summaries -------------------------------------------------------------


def _summarizing_cm(responses: list) -> ContextManager:
    return ContextManager(
        context_token_budget=2000,
        compaction_threshold=0.7,
        keep_fraction=0.5,
        summarizer=FakeClient(responses),
        utility_model="claude-sonnet-5",
    )


async def test_no_summary_when_under_budget() -> None:
    cm = _summarizing_cm([])
    cut, summary = await cm.summary_for(_conversation(2))
    assert cut == 0 and summary is None
    assert cm.summarizer.calls == []  # no summarization call


async def test_summary_generated_when_compacting() -> None:
    cm = _summarizing_cm([text_message("SUMMARY v1")])
    cut, summary = await cm.summary_for(_conversation(30))
    assert cut > 0
    assert summary == "SUMMARY v1"
    assert len(cm.summarizer.calls) == 1


async def test_summary_regenerates_only_when_cut_advances() -> None:
    cm = _summarizing_cm([text_message("SUMMARY v1"), text_message("SUMMARY v2")])
    full = _conversation(30)
    cut1, s1 = await cm.summary_for(full)
    assert s1 == "SUMMARY v1" and len(cm.summarizer.calls) == 1
    # same messages -> same cut -> cached, no new call
    cut2, s2 = await cm.summary_for(full)
    assert cut2 == cut1 and s2 == "SUMMARY v1" and len(cm.summarizer.calls) == 1
    # more conversation -> cut advances -> regenerate incrementally (prior folded in)
    _, s3 = await cm.summary_for(full + _conversation(20))
    assert s3 == "SUMMARY v2" and len(cm.summarizer.calls) == 2
    folded = cm.summarizer.calls[1]["messages"][0]["content"]
    assert "PRIOR SUMMARY" in folded and "SUMMARY v1" in folded


async def test_restore_avoids_resummarizing() -> None:
    cm = _summarizing_cm([text_message("should not be used")])
    full = _conversation(30)
    cut = cm._find_cut(full)
    cm.restore("restored summary", cut)
    rcut, summary = await cm.summary_for(full)
    assert rcut == cut and summary == "restored summary"
    assert cm.summarizer.calls == []  # nothing re-summarized


async def test_no_summarizer_still_compacts_without_summary() -> None:
    cm = ContextManager(context_token_budget=2000, compaction_threshold=0.7, keep_fraction=0.5)
    cut, summary = await cm.summary_for(_conversation(30))
    assert cut > 0  # prefix still dropped
    assert summary is None  # but no summary (degraded)


def test_state_roundtrips_through_restore() -> None:
    cm = ContextManager()
    cm.restore("s", 5)
    assert cm.state() == ("s", 5)
