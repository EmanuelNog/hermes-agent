"""Behavioural tests: a background-review fork compacts only when the request cannot be sent.

The review fork replays the parent's FULL snapshot as a warm cache read, so crossing the
summary threshold there buys nothing — the fork's transcript is thrown away when the run
ends. Measured on a 560k-token replay over a 1M window (threshold 500k): a full post-tool
summary ran for ~90 s of aux time and was then aborted, because a new live turn cancels the
review and that cancellation is the only thing that ever stops a fork mid-summary. Only not
*fitting* the window still justifies a pass on a fork.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_call(i: int):
    return SimpleNamespace(
        id=f"call_{i}",
        type="function",
        function=SimpleNamespace(name="web_search", arguments='{"query": "x"}'),
    )


def _tool_response(i: int):
    msg = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[_tool_call(i)],
    )
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _stop_response():
    msg = SimpleNamespace(
        content="done",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _pressured_compressor(prompt_tokens: int, *, window: int = 1_000_000) -> MagicMock:
    """A compressor that always reports pressure after tools run.

    ``should_defer_preflight_to_real_usage`` returns True so the turn-start preflight and the
    pre-API pressure gate stand down — isolating the post-tool gate as the only compression
    site under test. Threshold 50% of the window, as the live profile had it.
    """
    compressor = MagicMock()
    compressor.protect_first_n = 3
    compressor.protect_last_n = 20
    compressor.context_length = window
    compressor.threshold_tokens = window // 2
    compressor.last_prompt_tokens = prompt_tokens
    compressor.awaiting_real_usage_after_compression = False
    compressor.should_compress.return_value = True
    compressor.should_defer_preflight_to_real_usage.return_value = True
    compressor.get_active_compression_failure_cooldown.return_value = None
    return compressor


@pytest.fixture()
def agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=10,
        )
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a._disable_streaming = True
    a.tool_delay = 0
    a.save_trajectories = False
    a.compression_enabled = True
    # Skip the aux feasibility probe: it is a live provider round-trip, not this test's subject.
    a._compression_feasibility_checked = True
    a._compression_feasibility_route = None
    return a


def _as_review_fork(agent, prompt_tokens: int, *, window: int = 1_000_000):
    """The state ``_detach_fork_compression`` leaves on a real review fork."""
    agent._background_review_fork = True
    agent._review_defer_compaction_before_first_response = True
    agent.context_compressor = _pressured_compressor(prompt_tokens, window=window)
    return agent


def _run_tool_loop(agent: AIAgent, n_tool_iterations: int = 1):
    responses = [_tool_response(i) for i in range(n_tool_iterations)]
    responses.append(_stop_response())
    agent.client.chat.completions.create.side_effect = responses

    compress_calls = []

    def _fake_compress(messages, system_message, **kwargs):
        compress_calls.append(kwargs.get("trigger"))
        return messages, "compressed prompt"

    with (
        patch.object(agent, "_compress_context", side_effect=_fake_compress),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "model_tools.handle_function_call",
            lambda name, args, task_id=None, **kwargs: json.dumps({"ok": True}),
        ),
    ):
        result = agent.run_conversation("review the last turn")

    return result, compress_calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReviewForkCompactionScope:
    def test_fork_over_threshold_but_fitting_the_window_does_not_compact(self, agent):
        """560k of a 1M window over a 500k threshold: fits with headroom, so no summary."""
        _as_review_fork(agent, 560_000)

        result, compress_calls = _run_tool_loop(agent)

        assert result["completed"] is True
        assert compress_calls == [], (
            "a review fork must not spend a summary merely crossing the threshold; "
            f"got compaction(s) {compress_calls}"
        )

    def test_gate_still_compacts_a_normal_session_at_the_same_numbers(self, agent):
        """The same pressure on a live session must still compact — the scope is the fork."""
        agent.context_compressor = _pressured_compressor(560_000)

        result, compress_calls = _run_tool_loop(agent)

        assert result["completed"] is True
        assert compress_calls == ["post_tool"]

    def test_fork_whose_request_cannot_fit_still_compacts(self, agent):
        """Over the window, the fork has no choice: bound the snapshot."""
        _as_review_fork(agent, 1_200_000)

        result, compress_calls = _run_tool_loop(agent)

        assert result["completed"] is True
        assert compress_calls == ["post_tool"]


class TestReviewForkThresholdHelper:
    """The gate helper both automatic sites share."""

    def _helper(self, agent, tokens):
        from agent.turn_context import _review_fork_threshold_only

        return _review_fork_threshold_only(agent, tokens)

    def test_live_session_is_never_scoped_out(self, agent):
        agent.context_compressor = _pressured_compressor(560_000)
        assert self._helper(agent, 560_000) is False

    def test_fork_fitting_the_window_is_scoped_out(self, agent):
        _as_review_fork(agent, 560_000)
        assert self._helper(agent, 560_000) is True

    def test_fork_over_the_window_is_not_scoped_out(self, agent):
        _as_review_fork(agent, 1_200_000)
        assert self._helper(agent, 1_200_000) is False

    def test_fork_with_unknown_window_stays_conservative(self, agent):
        _as_review_fork(agent, 560_000)
        agent.context_compressor.context_length = None
        assert self._helper(agent, 560_000) is True


def test_detach_marks_the_fork():
    """``_detach_fork_compression`` must tag the fork so the gates can recognize it."""
    from agent.background_review import _detach_fork_compression

    fork = SimpleNamespace(context_compressor=MagicMock())
    _detach_fork_compression(fork)

    assert fork._background_review_fork is True


def test_prefetch_never_arms_on_a_review_fork(agent):
    """The async prefetch worker only pays off for a session that outlives the summary."""
    from agent.turn_prefetch_compaction import can_prefetch

    _as_review_fork(agent, 480_000)

    ok, reason = can_prefetch(agent, [{"role": "user", "content": "x"}], 480_000)

    assert ok is False
    assert reason == "review_fork"
