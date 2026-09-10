"""RED contracts for prefetch compaction (bead hermes-async-cwg.2).

These tests define the behaviour of the prefetch state machine BEFORE
it exists: trigger maths, the arming ballot, snapshot adoption with a grown
live tail, staleness discard, and the threshold-hit policy. They must fail
against the current checkout (module ``agent.turn_prefetch_compaction`` does not
exist yet) and go green with the implementation (hermes-async-cwg.3).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.context_compressor import _DB_PERSISTED_MARKER

import agent.turn_prefetch_compaction as tac  # noqa: F401  (RED: module missing)


# ---------------------------------------------------------------------------
# Trigger maths
# ---------------------------------------------------------------------------


def _compressor(*, context_length=64_000, threshold_tokens=5_120):
    return SimpleNamespace(context_length=context_length, threshold_tokens=threshold_tokens)


def test_trigger_disabled_when_margin_zero():
    # margin 0.0 = feature off; no trigger exists -> arming can never fire.
    assert tac.prefetch_trigger_tokens(_compressor(), 0.0) is None


def test_trigger_is_threshold_minus_margin_of_window():
    # user spec: threshold 8% of 64K = 5120; margin 0.01 -> arm at 4480 (7%).
    assert tac.prefetch_trigger_tokens(_compressor(), 0.01) == 4_480


def test_trigger_clamped_below_threshold_and_above_zero():
    c = _compressor(threshold_tokens=5_120)
    # A margin larger than the threshold itself must not produce a trigger at
    # or above the blocking threshold, nor a negative trigger.
    assert 0 <= tac.prefetch_trigger_tokens(c, 10.0) < c.threshold_tokens


@pytest.mark.parametrize(
    ("context_length", "threshold_tokens", "margin", "expected"),
    [
        (64_000, 5_120, 0.01, 4_480),   # user spec: 8% - 1% window
        (64_000, 40_000, 0.15, 30_400), # small-scale E2E band
        (1_000_000, 750_000, 0.05, 700_000),  # big validation band
        (32_000, 24_000, 0.15, 19_200),  # absolute-cap interplay
        (0, 5_120, 0.01, None),         # no context known -> no trigger
    ],
)
def test_trigger_parametrized(context_length, threshold_tokens, margin, expected):
    c = _compressor(context_length=context_length, threshold_tokens=threshold_tokens)
    assert tac.prefetch_trigger_tokens(c, margin) == expected


# ---------------------------------------------------------------------------
# Arming ballot
# ---------------------------------------------------------------------------


def _agent(**overrides):
    attrs = {
        "compression_enabled": True,
        "compression_prefetch_margin": 0.01,
        "prefetch_compaction_pending": None,
        "max_compression_attempts": 3,
        "compression_attempts": 0,
    }
    attrs.update(overrides)
    a = SimpleNamespace(**attrs)
    a.context_compressor = _compressor()
    a.context_compressor.get_active_compression_failure_cooldown = lambda: None
    return a


def _arm_msgs(n=40, persisted=True):
    msgs = []
    for i in range(n):
        m = {"role": "user" if i % 2 == 0 else "tool", "content": f"msg-{i}"}
        if persisted:
            m[_DB_PERSISTED_MARKER] = "db-persisted"
        msgs.append(m)
    return msgs


def test_ballot_arms_in_async_band():
    msgs = _arm_msgs()
    ok, reason = tac.can_prefetch(_agent(), msgs, tokens=4_600)
    assert ok and reason is None


def test_ballot_no_arm_below_trigger():
    ok, _ = tac.can_prefetch(_agent(), _arm_msgs(), tokens=4_000)
    assert not ok


def test_ballot_no_arm_at_or_above_threshold():
    # The blocking path owns tokens >= threshold; async must never steal them.
    ok, _ = tac.can_prefetch(_agent(), _arm_msgs(), tokens=5_120)
    assert not ok


def test_ballot_no_arm_while_pending():
    msgs = _arm_msgs()
    ok, _ = tac.can_prefetch(_agent(prefetch_compaction_pending=object()), msgs, tokens=4_600)
    assert not ok


def test_ballot_no_arm_while_failure_cooldown():
    a = _agent()
    a.context_compressor.get_active_compression_failure_cooldown = lambda: {"until": 1}
    ok, _ = tac.can_prefetch(a, _arm_msgs(), tokens=4_600)
    assert not ok


def test_ballot_no_arm_when_attempts_exhausted():
    a = _agent(compression_attempts=3)
    ok, _ = tac.can_prefetch(a, _arm_msgs(), tokens=4_600)
    assert not ok


def test_ballot_no_arm_with_unpersisted_rows():
    # The region the worker will archive must be fully durable BEFORE arming:
    # a concurrent main-thread flush must never overlap the worker's commit.
    msgs = _arm_msgs(n=40, persisted=True)
    msgs[-3] = {"role": "user", "content": "just-appended, not yet flushed"}
    ok, _ = tac.can_prefetch(_agent(), msgs, tokens=4_600)
    assert not ok


def test_ballot_no_arm_when_disabled():
    ok, _ = tac.can_prefetch(_agent(compression_enabled=False), _arm_msgs(), tokens=4_600)
    assert not ok


def test_ballot_no_arm_on_codex_app_server():
    # The codex agent owns its thread; async must not arm a compaction the
    # codex_app_server_auto policy would never sanction.
    a = _agent(api_mode="codex_app_server")
    ok, reason = tac.can_prefetch(a, _arm_msgs(), tokens=4_600)
    assert not ok and reason == "codex_app_server"


def test_ballot_no_arm_with_responses_native():
    # Server-side Responses compaction owns the window; a local async prefetch
    # would double-compact it.
    a = _agent(codex_responses_native_compaction=True)
    ok, reason = tac.can_prefetch(a, _arm_msgs(), tokens=4_600)
    assert not ok and reason == "responses_native"


def test_ballot_no_arm_in_rotation_mode():
    # in_place=false: the worker rotates session_id at commit and may absorb
    # freshly-flushed suffix rows; async is refused, blocking path stays.
    a = _agent(compression_in_place=False)
    ok, reason = tac.can_prefetch(a, _arm_msgs(), tokens=4_600)
    assert not ok and reason == "rotation_mode"


def test_pending_session_mismatch_clears_state():
    # A stale worker from a previous session must be dropped, not adopted.
    agent = _agent(session_id="new-session")
    agent.prefetch_compaction_pending = SimpleNamespace(
        session_id="old-session", fence=object(), future=None,
    )
    action, msgs = tac.run_prefetch_compaction_step(agent, _arm_msgs(), 4_600)
    assert action == "none" and msgs is not None
    assert agent.prefetch_compaction_pending is None


def test_await_failure_clears_state_and_allows_blocking():
    # A worker that FAILED while awaited must clear the pending state and return
    # 'none' so the blocking backstop can proceed — not 'pending' (which would
    # skip the blocking pass while over threshold).
    from concurrent.futures import Future

    agent = _agent()
    fut = Future()
    fut.set_exception(RuntimeError("worker exploded"))
    agent.prefetch_compaction_pending = SimpleNamespace(
        session_id="same-session", fence=object(), future=fut,
        snapshot=[], idle_timeout=1.0,
    )
    # tokens above threshold -> the step enters the await branch
    action, _msgs = tac.run_prefetch_compaction_step(agent, _arm_msgs(), 5_200)
    assert action == "none"
    assert agent.prefetch_compaction_pending is None


def test_pending_below_threshold_stays_pending():
    # Worker still running and context under the blocking threshold: keep going,
    # never block, never adopt.
    from concurrent.futures import Future

    agent = _agent()
    fut = Future()  # never resolved -> not done
    agent.prefetch_compaction_pending = SimpleNamespace(
        session_id=None, fence=object(), future=fut,
        snapshot=[], idle_timeout=1.0,
    )
    action, msgs = tac.run_prefetch_compaction_step(agent, _arm_msgs(), 4_600)
    assert action == "pending"
    assert agent.prefetch_compaction_pending is not None


# ---------------------------------------------------------------------------
# Adoption (snapshot result + grown live tail)
# ---------------------------------------------------------------------------


def _result_list():
    return (
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "user", "content": "tail-1"},
            {"role": "tool", "content": "tail-2"},
        ],
        "system-prompt",
    )


def test_adoption_splices_suffix_after_worker_result():
    live_arm = _arm_msgs(n=40, persisted=True)
    state = SimpleNamespace(snapshot=list(live_arm), arm_index=40, result=None)
    live = list(live_arm) + [
        {"role": "user", "content": "new-turn-1"},
        {"role": "tool", "content": "new-tool-result-1"},
    ]
    result = _result_list()
    new_msgs, adopted, reason = tac.adopt_prefetch_result(live, state, result)
    assert adopted and reason is None
    assert new_msgs[:4] == result[0]
    assert new_msgs[4:] == live[40:]


def test_adoption_dedupes_grew_before_lease_seam():
    # Rotation-mode "grew before lease": the worker's result tail absorbed the
    # freshly-flushed suffix rows, so the live suffix repeats them. The split must
    # drop the exact continuation, keeping only genuinely-new rows.
    live_arm = _arm_msgs(n=40, persisted=True)
    state = SimpleNamespace(snapshot=list(live_arm), arm_index=40, result=None)
    s0 = {"role": "user", "content": "suffix-0"}
    s1 = {"role": "tool", "content": "suffix-1"}
    s2 = {"role": "user", "content": "suffix-2-new"}
    live = list(live_arm) + [s0, s1, s2]
    # result tail = snapshot tail + absorbed copies of s0, s1
    result = (
        [{"role": "system", "content": "sys"},
         {"role": "user", "content": "[CONTEXT COMPACTION] summary"}] + live_arm[38:40] + [s0, s1],
        "system-prompt",
    )
    new_msgs, adopted, _ = tac.adopt_prefetch_result(live, state, result)
    assert adopted
    assert new_msgs == result[0] + [s2]
    assert new_msgs.count(s0) == 1 and new_msgs.count(s1) == 1


def test_adoption_keeps_distinct_live_tail_when_no_absorb():
    # Without durable absorption the dedupe is a no-op: distinct suffix rows stay.
    live_arm = _arm_msgs(n=40, persisted=True)
    state = SimpleNamespace(snapshot=list(live_arm), arm_index=40, result=None)
    s0 = {"role": "user", "content": "brand-new-a"}
    s1 = {"role": "tool", "content": "brand-new-b"}
    live = list(live_arm) + [s0, s1]
    result = (
        [{"role": "system", "content": "sys"},
         {"role": "user", "content": "[CONTEXT COMPACTION] summary"}],
        "system-prompt",
    )
    new_msgs, adopted, _ = tac.adopt_prefetch_result(live, state, result)
    assert adopted and new_msgs == result[0] + [s0, s1]


def test_adoption_no_growth_replaces_fully():
    live = _arm_msgs(n=40, persisted=True)
    state = SimpleNamespace(snapshot=list(live), arm_index=40, result=None)
    result = _result_list()
    new_msgs, adopted, _ = tac.adopt_prefetch_result(live, state, result)
    assert adopted and new_msgs == result[0]


def test_adoption_discards_when_prefix_diverged():
    # Rewind / edit-resend / another compaction changed history before arm.
    snap = _arm_msgs(n=40, persisted=True)
    state = SimpleNamespace(snapshot=snap, arm_index=40, result=None)
    live = list(snap)
    live[20] = {"role": "user", "content": "diverged-mid-history"}
    result = _result_list()
    new_msgs, adopted, reason = tac.adopt_prefetch_result(live, state, result)
    assert not adopted and new_msgs is live
    assert reason is not None


def test_adoption_discards_when_worker_nooped():
    # Worker returned the input list (abort/no-op): adopt must not corrupt.
    live = _arm_msgs(n=40, persisted=True)
    state = SimpleNamespace(snapshot=list(live), arm_index=40, result=None)
    new_msgs, adopted, _ = tac.adopt_prefetch_result(live, state, (live, ""))
    assert not adopted and new_msgs is live


# ---------------------------------------------------------------------------
# Threshold-hit policy
# ---------------------------------------------------------------------------


def test_policy_adopt_when_pending_done():
    act = tac.decide_threshold_action(
        pending_exists=True, pending_done=True, tokens=5_200, threshold_tokens=5_120
    )
    assert act == "adopt"


def test_policy_await_when_pending_not_done():
    act = tac.decide_threshold_action(
        pending_exists=True, pending_done=False, tokens=5_200, threshold_tokens=5_120
    )
    assert act == "await"


def test_policy_block_when_no_pending():
    act = tac.decide_threshold_action(
        pending_exists=False, pending_done=False, tokens=5_200, threshold_tokens=5_120
    )
    assert act == "block"


def test_policy_none_below_threshold_with_pending():
    act = tac.decide_threshold_action(
        pending_exists=True, pending_done=False, tokens=4_800, threshold_tokens=5_120
    )
    assert act == "none"