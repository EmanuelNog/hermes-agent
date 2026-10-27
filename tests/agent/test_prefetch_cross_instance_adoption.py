"""Cross-instance prefetch adoption (bead chk-n5va.32).

The armed worker outlives the AIAgent that launched it: the messaging gateway
evicts (or rebuilds) an agent between turns, so the adoption gate then runs on a
FRESH instance whose transcript was reloaded from the SessionDB. Two contracts
follow, and both were violated live in the isolated prefetchdev home ("Prefetch
compression armed" 11x, "Prefetch compaction adopted" 0x):

* the pending state must survive the agent boundary, otherwise the next gate
  cannot see the worker and simply arms a second, orphaned one;
* a worker whose commit is already the durable transcript must be recognised as
  ADOPTED — not discarded as "prefix_diverged" because the snapshot prefix is
  gone from a freshly reloaded list.
"""

from __future__ import annotations

from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from agent.context_compressor import SUMMARY_PREFIX, _DB_PERSISTED_MARKER

import agent.turn_prefetch_compaction as tac


@pytest.fixture(autouse=True)
def _fresh_registry():
    tac._PENDING_BY_SESSION.clear()
    yield
    tac._PENDING_BY_SESSION.clear()


def _compressor(*, context_length=64_000, threshold_tokens=22_000):
    c = SimpleNamespace(context_length=context_length, threshold_tokens=threshold_tokens)
    c.get_active_compression_failure_cooldown = lambda: None
    return c


def _agent(session_id="s-1", **overrides):
    attrs = {
        "session_id": session_id,
        "compression_enabled": True,
        "compression_prefetch_margin": 0.15,
        "prefetch_compaction_pending": None,
        "max_compression_attempts": 3,
        "compression_attempts": 0,
        "status_events": [],
    }
    attrs.update(overrides)
    a = SimpleNamespace(**attrs)
    a.context_compressor = _compressor()
    a._emit_status = lambda m: a.status_events.append(("lifecycle", m))
    a._emit_status_kind = lambda kind, msg, *, origin=None: a.status_events.append((kind, msg))
    a._emit_warning = lambda m: a.status_events.append(("warn", m))
    return a


def _persisted(content, role="user"):
    return {"role": role, "content": content, _DB_PERSISTED_MARKER: "db-persisted"}


def _summary_row(text="earlier work summarized"):
    """A compaction handoff row as it is stored: content prefix only (metadata is dropped)."""
    return {"role": "user", "content": f"{SUMMARY_PREFIX}\n\n{text}"}


def _done_state(snapshot, *, session_id="s-1", result):
    fut = Future()
    fut.set_result(result)
    return SimpleNamespace(
        session_id=session_id, fence=SimpleNamespace(), future=fut,
        snapshot=list(snapshot), arm_index=len(snapshot), idle_timeout=5.0,
        finished_at=None, started_at=None,
    )


def _running_state(snapshot, *, session_id="s-1"):
    return SimpleNamespace(
        session_id=session_id, fence=SimpleNamespace(), future=Future(),
        snapshot=list(snapshot), arm_index=len(snapshot), idle_timeout=5.0,
        finished_at=None, started_at=None,
    )


def test_fresh_agent_adopts_a_committed_prefetch_result():
    # The previous instance armed; its worker committed. The gateway dropped that
    # instance, so this agent's list is the DURABLE transcript: summary + the rows
    # that followed the commit. There is no snapshot prefix left to match.
    state = _done_state(
        [_persisted(f"old-{i}") for i in range(6)],
        result=([{"role": "system", "content": "sys"}, _summary_row()], "sys"),
    )
    tac._PENDING_BY_SESSION["s-1"] = state
    live = [_summary_row(), {"role": "assistant", "content": "ok"}, _persisted("next question")]

    fresh = _agent()
    action, msgs = tac.run_prefetch_compaction_step(fresh, live, 15_000)

    assert action == "adopted"
    assert msgs == live  # nothing to splice: the commit already IS the transcript
    assert fresh.prefetch_compaction_pending is None
    assert "s-1" not in tac._PENDING_BY_SESSION  # consumed, never adopted twice
    assert "compacted" in [kind for kind, _ in fresh.status_events]


def test_fresh_agent_sees_an_in_flight_worker_as_pending():
    # Still running across the instance boundary: the gate must NOT arm a second
    # worker nor block (the first one owns the session compression lease).
    state = _running_state([_persisted(f"old-{i}") for i in range(6)])
    tac._PENDING_BY_SESSION["s-1"] = state

    fresh = _agent()
    action, _msgs = tac.run_prefetch_compaction_step(fresh, list(state.snapshot), 15_000)

    assert action == "pending"
    assert fresh.prefetch_compaction_pending is state


def test_discards_when_the_transcript_is_not_our_commit():
    # A rewind / edit-resend / other compaction replaced the transcript: our
    # summary is nowhere in it, so the result is stale.
    state = _done_state(
        [_persisted(f"old-{i}") for i in range(6)],
        result=([{"role": "system", "content": "sys"}, _summary_row()], "sys"),
    )
    tac._PENDING_BY_SESSION["s-1"] = state
    rewritten = [{"role": "user", "content": "a different, unrelated history"}]

    fresh = _agent()
    action, msgs = tac.run_prefetch_compaction_step(fresh, rewritten, 5_000)

    assert action == "none"
    assert msgs == rewritten
    assert fresh.prefetch_compaction_pending is None


def test_registry_is_scoped_to_one_session():
    tac._PENDING_BY_SESSION["s-1"] = _done_state(
        [_persisted("x")], result=([_summary_row()], "sys"),
    )
    other = _agent(session_id="s-2")
    # Unpersisted rows: arming is refused, so no worker is launched either — the
    # only thing under test is that s-1's worker is invisible here.
    action, _msgs = tac.run_prefetch_compaction_step(other, [{"role": "user", "content": "x"}], 15_000)

    assert action == "none"


def test_launch_registers_the_pending_state_for_the_session(monkeypatch):
    import agent.conversation_compression as cc

    monkeypatch.setattr(cc, "_try_admit_compression_job", lambda: True)
    monkeypatch.setattr(cc, "_release_compression_admission", lambda *_a, **_k: None)

    class _PendingExec:
        """Never runs the worker: the arming side effect is what this pins."""

        def submit(self, fn, *a, **k):
            return Future()

    monkeypatch.setattr(cc, "_get_compress_timeout_executor", lambda: _PendingExec())

    agent = _agent()
    state = tac.launch_prefetch_compression(agent, [_persisted("a")], "sys", 15_000)

    assert state is not None
    assert tac._PENDING_BY_SESSION.get("s-1") is state


def test_launch_refuses_a_second_worker_for_a_registered_session(monkeypatch):
    import agent.conversation_compression as cc

    monkeypatch.setattr(cc, "_try_admit_compression_job", lambda: True)
    monkeypatch.setattr(cc, "_release_compression_admission", lambda *_a, **_k: None)

    class _PendingExec:
        def submit(self, fn, *a, **k):
            return Future()

    monkeypatch.setattr(cc, "_get_compress_timeout_executor", lambda: _PendingExec())

    tac._PENDING_BY_SESSION["s-1"] = _running_state([_persisted("a")])
    fresh = _agent()
    assert tac.launch_prefetch_compression(fresh, [_persisted("a")], "sys", 15_000) is None
