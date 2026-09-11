"""Behaviour contracts for prefetch compaction (bead hermes-async-cwg).

Trigger maths, the arming ballot, in-process arming -> adoption, staleness
discard, the threshold-hit policy, and fence ownership. The upstream seams this
module leans on are pinned separately in test_prefetch_upstream_compat.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.context_compressor import _DB_PERSISTED_MARKER

import agent.turn_prefetch_compaction as tac


# ---------------------------------------------------------------------------
# Trigger maths
# ---------------------------------------------------------------------------


def _compressor(*, context_length=64_000, threshold_tokens=5_120):
    return SimpleNamespace(context_length=context_length, threshold_tokens=threshold_tokens)


def test_trigger_disabled_when_margin_zero():
    # margin 0.0 = feature off; no trigger exists -> arming can never fire.
    assert tac.prefetch_trigger_tokens(_compressor(), 0.0) is None


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
        (1_000, 5_120, 0.0005, 5_119),  # sub-unit margin -> clamp to thr-1
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
    # Status recorders (mirror StatusOutputMixin's native surfaces).
    a.status_events = []
    a._emit_status = lambda m: a.status_events.append(("lifecycle", m))
    a._emit_status_kind = lambda kind, msg, *, origin=None: a.status_events.append((kind, msg))
    a._emit_warning = lambda m: a.status_events.append(("warn", m))
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


def test_done_failure_discards_state_and_allows_blocking():
    # Worker future finished WITH an exception: the done() branch logs, clears
    # the pending state and returns 'none' so the blocking backstop can proceed.
    from concurrent.futures import Future

    agent = _agent()
    fut = Future()
    fut.set_exception(RuntimeError("worker exploded"))
    agent.prefetch_compaction_pending = SimpleNamespace(
        session_id=None, fence=object(), future=fut, snapshot=[], idle_timeout=1.0,
    )
    action, _msgs = tac.run_prefetch_compaction_step(agent, _arm_msgs(), 5_200)
    assert action == "none"
    assert agent.prefetch_compaction_pending is None
    # Failure-class notice with the elapsed time.
    assert any(k == "warn" and "Prefetch compaction failed after" in m for k, m in agent.status_events)


class _DuckFuture:
    """Future whose done() stays False; result() raises on first call.

    Lets the await branch be tested without a real 5s wait.
    """

    def __init__(self, exc):
        self._exc = exc

    def done(self):
        return False

    def result(self, timeout=None):
        raise self._exc

    def add_done_callback(self, cb):  # pragma: no cover - not reached
        pass


def test_await_branch_failure_clears_state():
    # Worker fails WHILE being awaited (not earlier): must clear state and let
    # the blocking path run this pass.
    agent = _agent()
    agent.prefetch_compaction_pending = SimpleNamespace(
        session_id=None, fence=object(),
        future=_DuckFuture(RuntimeError("late failure")), snapshot=[], idle_timeout=1.0,
    )
    action, _msgs = tac.run_prefetch_compaction_step(agent, _arm_msgs(), 5_200)
    assert action == "none"
    assert agent.prefetch_compaction_pending is None
    assert any(k == "warn" and "Prefetch compaction failed after" in m for k, m in agent.status_events)


def test_await_branch_timeout_keeps_pending():
    # Worker still running at the wait deadline: stay 'pending' (worker owns the
    # session lease; the blocking pass will lock-skip), never clear.
    from concurrent.futures import TimeoutError as _FT

    agent = _agent()
    agent.prefetch_compaction_pending = SimpleNamespace(
        session_id=None, fence=object(),
        future=_DuckFuture(_FT("still running")), snapshot=[], idle_timeout=1.0,
    )
    action, _msgs = tac.run_prefetch_compaction_step(agent, _arm_msgs(), 5_200)
    assert action == "pending"
    assert agent.prefetch_compaction_pending is not None


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


# ---------------------------------------------------------------------------
# Ballot reasons (distinct refusal contracts)
# ---------------------------------------------------------------------------


def test_ballot_reason_margin_zero():
    ok, reason = tac.can_prefetch(_agent(compression_prefetch_margin=0.0), _arm_msgs(), 4_600)
    assert not ok and reason == "margin_zero"


def test_ballot_reason_no_compressor():
    a = _agent()
    a.context_compressor = None
    ok, reason = tac.can_prefetch(a, _arm_msgs(), 4_600)
    assert not ok and reason == "no_compressor"


def test_ballot_reason_no_trigger():
    a = _agent()
    a.context_compressor = _compressor(context_length=0)
    ok, reason = tac.can_prefetch(a, _arm_msgs(), 4_600)
    assert not ok and reason == "no_trigger"


# ---------------------------------------------------------------------------
# Launch + in-process arm -> adopt
# ---------------------------------------------------------------------------


def _fake_cc(monkeypatch, *, admit=True, result=None):
    """Monkeypatch the conversation_compression seams the launch uses.

    The fake executor runs the submitted worker SYNCHRONOUSLY and returns a
    resolved Future, so arming + adoption are exercised in-process with no
    thread.
    """
    import agent.conversation_compression as cc
    from concurrent.futures import Future

    st = {"released": 0, "calls": []}

    monkeypatch.setattr(cc, "_try_admit_compression_job", lambda: admit)

    def _release(*_a):
        st["released"] += 1

    monkeypatch.setattr(cc, "_release_compression_admission", _release)

    class FakeExec:
        def submit(self, fn, *a, **k):
            out = fn(*a, **k)
            f = Future()
            f.set_result(out)
            return f

    monkeypatch.setattr(cc, "_get_compress_timeout_executor", lambda: FakeExec())

    if result is not None:
        def fake_compress(agent, messages, system_message, approx_tokens=None,
                          task_id=None, defer_context_engine_notification=False,
                          commit_fence=None):
            st["calls"].append(
                {"approx_tokens": approx_tokens, "task_id": task_id, "commit_fence": commit_fence}
            )
            return result

        monkeypatch.setattr(cc, "compress_context", fake_compress)
    return st


def test_launch_arms_publishes_fence_and_releases_admission(monkeypatch):
    result = ([{"role": "user", "content": "compacted"}], "sys")
    st = _fake_cc(monkeypatch, result=result)
    agent = _agent()
    msgs = _arm_msgs(40)
    state = tac.launch_prefetch_compression(agent, msgs, "sys-prompt", 4_600, task_id="t1")
    assert state is not None
    assert agent.prefetch_compaction_pending is state
    assert agent._active_compression_commit_fence is state.fence
    assert state.snapshot == msgs and state.snapshot is not msgs  # frozen shallow copy
    assert state.arm_index == 40 and state.armed_tokens == 4_600
    assert st["released"] == 1  # done-callback must release the admission
    assert st["calls"][0]["approx_tokens"] == 4_600
    assert st["calls"][0]["task_id"] == "t1"
    assert st["calls"][0]["commit_fence"] is state.fence
    # Native-style start report for the user.
    assert [k for k, _ in agent.status_events] == ["lifecycle"]
    _k, _m = agent.status_events[0]
    assert "Compacting context (prefetch)" in _m and "~4,600 tokens" in _m


def test_launch_refused_when_already_pending(monkeypatch):
    st = _fake_cc(monkeypatch, result=([], ""))
    agent = _agent(prefetch_compaction_pending=object())
    assert tac.launch_prefetch_compression(agent, _arm_msgs(), "s", 4_600) is None
    assert st["calls"] == []


def test_launch_refused_when_pool_saturated(monkeypatch):
    st = _fake_cc(monkeypatch, admit=False, result=([], ""))
    agent = _agent()
    assert tac.launch_prefetch_compression(agent, _arm_msgs(), "s", 4_600) is None
    assert st["calls"] == []
    assert st["released"] == 0  # never admitted -> nothing to release
    assert agent.status_events == []  # refusals are silent, natively


def test_step_arm_then_adopt_end_to_end(monkeypatch):
    # The full in-process flow: gate 1 arms, gate 2 (worker already done) adopts.
    result = ([{"role": "user", "content": "compacted"}], "sys")
    _fake_cc(monkeypatch, result=result)
    agent = _agent()
    msgs = _arm_msgs(40)

    action1, msgs1 = tac.run_prefetch_compaction_step(
        agent, msgs, 4_600, system_message="s", task_id="t1"
    )
    assert action1 == "armed" and msgs1 is msgs
    assert agent.prefetch_compaction_pending is not None

    action2, msgs2 = tac.run_prefetch_compaction_step(
        agent, msgs, 4_600, system_message="s", task_id="t1"
    )
    assert action2 == "adopted"
    assert msgs2 == result[0]
    assert agent.prefetch_compaction_pending is None
    assert agent._last_compaction_in_place is True
    assert not hasattr(agent, "_active_compression_commit_fence")  # fence unregistered
    # Start + terminal edge reported in native registers (counts + duration).
    kinds = [k for k, _ in agent.status_events]
    assert kinds == ["lifecycle", "compacted"]
    assert "40 → 1 messages in 0s" in agent.status_events[1][1]


def test_clear_pending_state_fence_ownership():
    agent = _agent()
    ours, other = object(), object()

    # A newer pass owns the active fence: clearing must NOT pop it.
    agent.prefetch_compaction_pending = SimpleNamespace(fence=ours)
    agent._active_compression_commit_fence = other
    tac._clear_pending_state(agent)
    assert agent._active_compression_commit_fence is other

    # Still ours: clear it out.
    agent.prefetch_compaction_pending = SimpleNamespace(fence=ours)
    agent._active_compression_commit_fence = ours
    tac._clear_pending_state(agent)
    assert not hasattr(agent, "_active_compression_commit_fence")


# ---------------------------------------------------------------------------
# Adoption edge cases
# ---------------------------------------------------------------------------


def test_adoption_rejects_non_tuple_result():
    live = _arm_msgs()
    state = SimpleNamespace(snapshot=list(live), arm_index=40, result=None)
    new_msgs, adopted, reason = tac.adopt_prefetch_result(live, state, ["not-a-tuple"])
    assert not adopted and new_msgs is live and reason == "bad_result"


def test_adoption_worker_noop_when_result_is_snapshot_identity():
    live = _arm_msgs()
    snap = list(live)
    state = SimpleNamespace(snapshot=snap, arm_index=40, result=None)
    new_msgs, adopted, reason = tac.adopt_prefetch_result(live, state, (snap, "s"))
    assert not adopted and new_msgs is live and reason == "worker_noop"


def test_adoption_worker_noop_on_equal_copy():
    # Deep-equal but distinct list of the same length: still a no-op, not a
    # "splice" that would silently rewrite identical history.
    live = _arm_msgs()
    state = SimpleNamespace(snapshot=list(live), arm_index=40, result=None)
    copy_of_snap = [dict(m) for m in live]
    new_msgs, adopted, reason = tac.adopt_prefetch_result(live, state, (copy_of_snap, "s"))
    assert not adopted and new_msgs is live and reason == "worker_noop"


# ---------------------------------------------------------------------------
# Worker pre-cancel, submit failure, saturated step, done/await discards
# ---------------------------------------------------------------------------


def test_worker_returns_input_when_fence_precancelled(monkeypatch):
    # A /stop between arm and worker start: the worker must return the input
    # untouched WITHOUT touching compress_context, and adoption must discard.
    import agent.conversation_compression as cc

    _fake_cc(monkeypatch)
    called = []
    monkeypatch.setattr(cc, "compress_context", lambda *a, **k: called.append(1))

    class FakeFence:
        deadline_exceeded = True
        is_cancelled = False

        def set_total_ceiling_seconds(self, s):
            self.ceiling = s

    monkeypatch.setattr(cc, "CompressionCommitFence", FakeFence)
    agent = _agent()
    msgs = _arm_msgs(40)
    state = tac.launch_prefetch_compression(agent, msgs, "s", 4_600)
    assert state is not None and called == []

    action, out = tac.run_prefetch_compaction_step(agent, msgs, 4_600)
    assert action == "none" and out is msgs  # no-op result -> discard, no corruption


def test_launch_releases_admission_when_submit_raises(monkeypatch):
    import agent.conversation_compression as cc

    st = _fake_cc(monkeypatch)

    class BoomExec:
        def submit(self, *a, **k):
            raise RuntimeError("pool dead")

    monkeypatch.setattr(cc, "_get_compress_timeout_executor", lambda: BoomExec())
    agent = _agent()
    with pytest.raises(RuntimeError):
        tac.launch_prefetch_compression(agent, _arm_msgs(), "s", 4_600)
    assert st["released"] == 1  # admission must not leak on submit failure
    assert agent.prefetch_compaction_pending is None


def test_step_stays_none_when_pool_saturated(monkeypatch):
    # Ballot passes but the pool refuses: the step reports 'none' so the caller
    # keeps the normal (non-compressing) behaviour this pass.
    _fake_cc(monkeypatch, admit=False)
    agent = _agent()
    action, _ = tac.run_prefetch_compaction_step(agent, _arm_msgs(), 4_600)
    assert action == "none" and agent.prefetch_compaction_pending is None


def test_step_none_when_ballot_refuses():
    # Feature off (margin 0): the step must be a pass-through with the caller's
    # list untouched.
    agent = _agent(compression_prefetch_margin=0.0)
    msgs = _arm_msgs()
    action, out = tac.run_prefetch_compaction_step(agent, msgs, 4_600)
    assert action == "none" and out is msgs


# ---------------------------------------------------------------------------
# Duration formatting for the user-facing reports
# ---------------------------------------------------------------------------


def test_elapsed_seconds_uses_finished_and_clamps():
    assert tac._prefetch_elapsed_seconds(
        SimpleNamespace(started_at=100.0, finished_at=110.6)
    ) == 11
    # Clock skew / missing stamps must never produce a negative or raise.
    assert tac._prefetch_elapsed_seconds(
        SimpleNamespace(started_at=110.0, finished_at=100.0)
    ) == 0
    assert tac._prefetch_elapsed_seconds(SimpleNamespace()) == 0


def test_elapsed_seconds_tolerates_garbage_start():
    assert tac._prefetch_elapsed_seconds(
        SimpleNamespace(started_at="garbage", finished_at=100.0)
    ) == 0


def test_done_branch_discards_diverged_result(monkeypatch):
    _fake_cc(monkeypatch, result=([{"role": "user", "content": "compact"}], "s"))
    agent = _agent()
    msgs = _arm_msgs(40)
    assert tac.run_prefetch_compaction_step(agent, msgs, 4_600)[0] == "armed"
    msgs[20] = {"role": "user", "content": "diverged"}  # edit-resend after arm
    action, out = tac.run_prefetch_compaction_step(agent, msgs, 4_600)
    assert action == "none" and out is msgs
    assert agent.prefetch_compaction_pending is None
    # Start was reported; the stale discard itself is silent (no done, no failure).
    assert [k for k, _ in agent.status_events] == ["lifecycle"]


class _DuckReturns:
    """Future whose done() stays False; result() RETURNS a value."""

    def __init__(self, value):
        self.value = value

    def done(self):
        return False

    def result(self, timeout=None):
        return self.value

    def add_done_callback(self, cb):  # pragma: no cover - not reached
        pass


def test_await_branch_adopts_completed_result():
    live = _arm_msgs(40)
    snap = list(live)
    result = ([{"role": "user", "content": "compact"}], "s")
    agent = _agent()
    agent.prefetch_compaction_pending = SimpleNamespace(
        session_id=None, fence=object(), future=_DuckReturns(result),
        snapshot=snap, idle_timeout=1.0,
    )
    action, out = tac.run_prefetch_compaction_step(agent, live, 5_200)
    assert action == "adopted" and out == result[0]
    assert agent.prefetch_compaction_pending is None
    assert agent._last_compaction_in_place is True
    assert any(k == "compacted" and "Prefetch compaction complete" in m for k, m in agent.status_events)


def test_await_branch_discards_failed_adoption():
    msgs = _arm_msgs(40)
    snap = list(msgs)
    msgs[5] = {"role": "user", "content": "diverged"}
    result = ([{"role": "user", "content": "compact"}], "s")
    agent = _agent()
    agent.prefetch_compaction_pending = SimpleNamespace(
        session_id=None, fence=object(), future=_DuckReturns(result),
        snapshot=snap, idle_timeout=1.0,
    )
    action, out = tac.run_prefetch_compaction_step(agent, msgs, 5_200)
    assert action == "none" and out is msgs
    assert agent.prefetch_compaction_pending is None
    # A stale discard is silent, natively (no done, no failure notice).
    assert agent.status_events == []