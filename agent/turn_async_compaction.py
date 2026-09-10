"""Async-threshold compaction (hermes-async-cwg): non-blocking summary prefetch.

When the transcript reaches ``threshold_tokens - async_margin * context_length``
(margin as a fraction of the WINDOW, default 0 = disabled), the summary worker
is launched WITHOUT blocking the loop. The loop keeps executing; at a later gate
the finished result is adopted: the worker's compacted prefix plus the live list
suffix that grew after arming (prefix identity-checked against the frozen
snapshot — a rewind, edit-resend or competing compaction discards the result as
stale). The blocking compression path is untouched: tokens >= threshold with no
pending worker behaves exactly as before; tokens >= threshold with a pending
worker adopts if done, otherwise waits a bounded slice and then degrades like a
timeout (the in-flight worker still owns the session compression lease).

Invariant: arming requires EVERY message to carry the persisted marker, so the
worker's SessionDB commit (it runs ``compress_context`` with its own commit
fence) can never overlap a main-thread flush of the same rows.

Scope: automatic in-loop compaction only. Manual /compress, gateway session
hygiene, Codex native and Responses-native paths are untouched.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DB_PERSISTED_MARKER_CACHE: Optional[str] = None


def _persisted_marker() -> str:
    global _DB_PERSISTED_MARKER_CACHE
    if _DB_PERSISTED_MARKER_CACHE is None:
        from agent.context_compressor import _DB_PERSISTED_MARKER as marker
        _DB_PERSISTED_MARKER_CACHE = marker
    return _DB_PERSISTED_MARKER_CACHE


def async_trigger_tokens(compressor: Any, margin: float) -> Optional[int]:
    """Prompt-token level that arms the async worker; None when disabled.

    ``margin`` is a fraction of the context WINDOW subtracted from the blocking
    threshold (user spec: threshold 8% of 64K = 5120, margin 0.01 -> 4480 = 7%).
    Clamped to [0, threshold_tokens - 1] so the blocking path always owns the
    threshold itself.
    """
    if margin is None or margin <= 0:
        return None
    ctx = int(getattr(compressor, "context_length", 0) or 0)
    thr = int(getattr(compressor, "threshold_tokens", 0) or 0)
    if ctx <= 0 or thr <= 0:
        return None
    trigger = thr - int(margin * ctx)
    if trigger < 0:
        trigger = 0
    if trigger >= thr:
        trigger = max(thr - 1, 0)
    return trigger


def region_fully_persisted(messages: List[dict]) -> bool:
    """Every row must already be durable before the worker can archive it."""
    marker = _persisted_marker()
    for m in messages:
        if not isinstance(m, dict) or not m.get(marker):
            return False
    return True


def can_arm_async(agent: Any, messages: List[dict], tokens: int) -> Tuple[bool, Optional[str]]:
    """Ballot for arming the async worker at the current token level."""
    if not getattr(agent, "compression_enabled", False):
        return False, "disabled"
    # Codex app-server threads are owned by the codex agent; Hermes must never start
    # compression for them outside the codex_app_server_auto policy (compress_context
    # would route to the app-server's thread compaction behind that switch).
    if getattr(agent, "api_mode", None) == "codex_app_server":
        return False, "codex_app_server"
    # Server-side Responses compaction (gpt-5.6) compacts at ~threshold - 8K with its
    # own opaque checkpoints; a local async prefetch at margin below threshold would
    # double-compact the same window. Keep the classic blocking fallback, kill async.
    if getattr(agent, "codex_responses_native_compaction", False):
        return False, "responses_native"
    margin = float(getattr(agent, "compression_async_margin", 0.0) or 0.0)
    if margin <= 0:
        return False, "margin_zero"
    if getattr(agent, "async_compaction_pending", None) is not None:
        return False, "already_pending"
    compressor = getattr(agent, "context_compressor", None)
    if compressor is None:
        return False, "no_compressor"
    trigger = async_trigger_tokens(compressor, margin)
    if trigger is None:
        return False, "no_trigger"
    thr = int(getattr(compressor, "threshold_tokens", 0) or 0)
    if tokens < trigger or tokens >= thr:
        return False, "not_in_async_band"
    cooldown = getattr(compressor, "get_active_compression_failure_cooldown", None)
    if callable(cooldown) and cooldown():
        return False, "failure_cooldown"
    attempts = int(getattr(agent, "compression_attempts", 0) or 0)
    max_attempts = max(1, int(getattr(agent, "max_compression_attempts", 3) or 3))
    if attempts >= max_attempts:
        return False, "attempts_exhausted"
    if not region_fully_persisted(messages):
        return False, "unpersisted_rows"
    return True, None


def decide_threshold_action(
    pending_exists: bool, pending_done: bool, tokens: int, threshold_tokens: int,
) -> str:
    """Threshold-hit policy: 'adopt' | 'await' | 'block' | 'none'."""
    if tokens < threshold_tokens:
        return "none"
    if not pending_exists:
        return "block"
    return "adopt" if pending_done else "await"


def adopt_async_result(
    live_messages: List[dict], state: Any, result: Any,
) -> Tuple[List[dict], bool, Optional[str]]:
    """Splice a finished worker result onto the live list.

    Returns ``(new_messages, adopted, reason)``. The worker result (a fresh
    list) replaces the frozen snapshot prefix; everything the live list gained
    after arming stays verbatim. Any divergence of the live prefix from the
    snapshot discards the result — the seam must be byte-identical or the
    summary is stale.
    """
    if not (isinstance(result, tuple) and result and isinstance(result[0], list)):
        return live_messages, False, "bad_result"
    result_messages = result[0]
    if result_messages is live_messages or result_messages is state.snapshot:
        return live_messages, False, "worker_noop"
    snap = state.snapshot
    n = len(snap)
    if n == 0 or n > len(live_messages) or live_messages[:n] != snap:
        return live_messages, False, "prefix_diverged"
    if len(result_messages) == n and result_messages == snap:
        return live_messages, False, "worker_noop"
    return result_messages + live_messages[n:], True, None


def launch_async_compression(
    agent: Any, messages: List[dict], system_message: str, tokens: int,
    task_id: str = "default",
) -> Optional[Any]:
    """Submit the snapshot worker on the shared compression pool WITHOUT awaiting.

    Returns the pending state (stored on ``agent.async_compaction_pending``) or
    None when the pool is saturated. The worker deep-copies the frozen shallow
    snapshot and runs ``compress_context`` with its own commit fence (durable
    SessionDB mutation happens on the worker; the main thread never touches it).
    """
    if getattr(agent, "async_compaction_pending", None) is not None:
        return None
    from agent.conversation_compression import (
        CompressionCommitFence,
        _get_compress_timeout_executor,
        _release_compression_admission,
        _try_admit_compression_job,
        resolve_context_compression_timeouts,
    )
    if not _try_admit_compression_job():
        logger.warning(
            "Async-threshold compression: pool saturated — skipping arm this cycle (session %s)",
            getattr(agent, "session_id", None) or "none",
        )
        return None
    idle_timeout, total_ceiling = resolve_context_compression_timeouts()
    fence = CompressionCommitFence()
    fence.set_total_ceiling_seconds(max(float(total_ceiling), float(idle_timeout)))
    # Shallow freeze: the live list may keep growing; the snapshot never does.
    snapshot = list(messages)
    arm_index = len(messages)

    def _async_worker(worker_fence: CompressionCommitFence) -> Tuple[list, str]:
        if worker_fence.deadline_exceeded or worker_fence.is_cancelled:
            return messages, ""
        from agent.conversation_compression import compress_context
        snap_copy = copy.deepcopy(snapshot)
        return compress_context(
            agent, snap_copy, system_message, approx_tokens=tokens, task_id=task_id,
            defer_context_engine_notification=False, commit_fence=worker_fence,
        )

    from tools.thread_context import propagate_context_to_thread
    try:
        future = _get_compress_timeout_executor().submit(
            propagate_context_to_thread(_async_worker), fence
        )
    except BaseException:
        _release_compression_admission()
        raise
    future.add_done_callback(_release_compression_admission)
    state = SimpleNamespace(
        future=future, fence=fence, snapshot=snapshot, arm_index=arm_index,
        armed_tokens=tokens, started_at=time.monotonic(), idle_timeout=float(idle_timeout),
        session_id=getattr(agent, "session_id", None),
    )
    agent.async_compaction_pending = state
    # Publish the fence so hard_interrupt() (/stop) can cancel this worker
    # pre-commit, mirroring the blocking facade's registration.
    fence_registration_lock = vars(agent).setdefault(
        "_compression_commit_fence_lock", threading.RLock()
    )
    with fence_registration_lock:
        agent._active_compression_commit_fence = fence
    logger.info(
        "Async-threshold compression armed at ~%s tokens (threshold %s, margin %s, context %s) "
        "session %s",
        f"{tokens:,}",
        f"{int(getattr(agent.context_compressor, 'threshold_tokens', 0) or 0):,}",
        f"{float(getattr(agent, 'compression_async_margin', 0.0) or 0.0):g}",
        f"{int(getattr(agent.context_compressor, 'context_length', 0) or 0):,}",
        getattr(agent, "session_id", None) or "none",
    )
    return state


def _clear_pending_state(agent: Any) -> None:
    """Drop the pending state and unregister OUR published fence.

    Only removes the fence when it is still ours — a newer pass (blocking
    compress, another async arm) publishes its own over it and must stay.
    """
    state = getattr(agent, "async_compaction_pending", None)
    agent.async_compaction_pending = None
    if state is not None:
        fence = getattr(state, "fence", None)
        if fence is not None:
            with vars(agent).setdefault("_compression_commit_fence_lock", threading.RLock()):
                if vars(agent).get("_active_compression_commit_fence") is fence:
                    vars(agent).pop("_active_compression_commit_fence", None)


def run_async_compaction_step(
    agent: Any, messages: List[dict], tokens: int, *, system_message: Any = "",
    task_id: str = "default",
) -> Tuple[str, List[dict]]:
    """One async-threshold step at a gate. Returns ``(action, messages)``.

    actions:
      'adopted' — a pending worker finished and its result was spliced; the
                  caller MUST re-baseline conversation history (and skip the
                  blocking compression branch this pass).
      'armed'   — an async worker was launched; continue WITHOUT blocking.
      'pending' — a worker is still running; continue (caller must NOT launch
                  or block on a competing compression this pass).
      'none'    — nothing to do; the normal (blocking) path applies.
    """
    state = getattr(agent, "async_compaction_pending", None)
    if state is None:
        can, _reason = can_arm_async(agent, messages, tokens)
        if not can:
            return "none", messages
        launched = launch_async_compression(
            agent, messages, system_message, tokens, task_id=task_id
        )
        return ("armed" if launched is not None else "none"), messages

    # Pending worker: check session identity first (a stale worker from a
    # previous session must never be adopted).
    if getattr(state, "session_id", None) not in (None, getattr(agent, "session_id", None)):
        _clear_pending_state(agent)
        return "none", messages
    if state.future.done():
        _clear_pending_state(agent)
        try:
            result = state.future.result()
        except Exception as exc:  # worker raised after done() — cooldown already recorded
            logger.warning("Async-threshold worker failed: %s", exc)
            return "none", messages
        new_messages, adopted, reason = adopt_async_result(messages, state, result)
        if adopted:
            agent._last_compaction_in_place = bool(getattr(agent, "compression_in_place", True))
            logger.info(
                "Async-threshold compaction adopted: %d -> %d messages, %d live suffix rows kept "
                "(session %s)",
                len(state.snapshot), len(new_messages), len(messages) - len(state.snapshot),
                getattr(agent, "session_id", None) or "none",
            )
            return "adopted", new_messages
        if reason:
            logger.info("Async-threshold result discarded (%s)", reason)
        return "none", messages

    # Still running: threshold-hit policy — bounded wait once, then degrade.
    thr = int(getattr(agent.context_compressor, "threshold_tokens", 0) or 0)
    action = decide_threshold_action(True, False, tokens, thr)
    if action == "await":
        wait_budget = min(max(float(getattr(state, "idle_timeout", 0.0)) * 2.0, 5.0), 60.0)
        try:
            result = state.future.result(timeout=wait_budget)
        except Exception:
            logger.info(
                "Async-threshold worker still running after %.0fs at threshold; "
                "degrading (blocking pass will lock-skip on its lease)",
                wait_budget,
            )
            return "pending", messages
        _clear_pending_state(agent)
        new_messages, adopted, reason = adopt_async_result(messages, state, result)
        if adopted:
            agent._last_compaction_in_place = bool(getattr(agent, "compression_in_place", True))
            return "adopted", new_messages
        if reason:
            logger.info("Async-threshold result discarded after await (%s)", reason)
        return "none", messages
    return "pending", messages