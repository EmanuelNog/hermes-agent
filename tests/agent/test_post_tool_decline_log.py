"""A post-tool compaction the gate declines at/above the trigger must say so, once per turn.

Measured in chk-n5va.98: a live session kept sending 550K-token requests with no compaction
attempt and NOTHING in agent.log/gateway.log/errors.log — a silent decline is indistinguishable
from a healthy turn, so the branch that went quiet could not be named. These tests pin one INFO
line per turn, naming the branch, and none below the trigger.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from agent.turn_preflight import compress_after_tool_results

THRESHOLD = 1_000
ABOVE = 5_000


def _messages() -> list[dict]:
    return [
        {"role": "user", "content": "x" * 800},
        {"role": "assistant", "content": "y" * 800},
        {"role": "user", "content": "current ask"},
    ]


def _agent(*, enabled: bool = True, awaiting: bool = False,
           gate: tuple[bool, str | None] = (True, None)) -> SimpleNamespace:
    """A minimal agent double: over the trigger (last_prompt_tokens), declining by `enabled`."""
    compressor = SimpleNamespace(
        threshold_tokens=THRESHOLD, context_length=200_000, last_prompt_tokens=ABOVE,
        awaiting_real_usage_after_compression=awaiting,
        should_compress=lambda _tokens: gate[0],
        should_compress_info=lambda _tokens: gate,
    )
    return SimpleNamespace(
        context_compressor=compressor,
        compression_enabled=enabled,
        model="m",
        session_id="s1",
        tools=[],
        _usage_anchor=None,
        _user_turn_count=7,
        _safe_print=lambda *_a: None,
        _emit_status=lambda _m: None,
        _emit_warning=lambda _m: None,
        _warn_context_overflow_blocked=lambda *_a, **_k: None,
    )


def _gate(agent: SimpleNamespace, messages: list[dict]) -> None:
    compress_after_tool_results(
        agent, messages=messages, system_message="sys", user_message="current ask",
        active_system_prompt="sys", conversation_history=None, compression_attempts=0,
        max_compression_attempts=3, effective_task_id="task", final_response=None,
        turn_exit_reason=None, current_turn_user_idx=len(messages) - 1,
    )


def _declines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "post-tool compression declined" in r.getMessage()]


def test_disabled_compression_above_the_trigger_records_the_decline(caplog):
    agent, messages = _agent(enabled=False), _messages()
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        _gate(agent, messages)
    records = _declines(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    line = records[0].getMessage()
    assert "compression_disabled" in line and str(ABOVE) in line and str(THRESHOLD) in line


def test_the_stuck_latch_records_the_decline(caplog):
    """The awaiting-real-usage latch used to return before anything inspected the token count."""
    agent, messages = _agent(awaiting=True), _messages()
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        _gate(agent, messages)
    records = _declines(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert "awaiting_real_usage_after_compression" in records[0].getMessage()


def test_a_blocked_gate_names_the_block_kind(caplog):
    """The incident's shape: an in-memory cooldown/backoff left armed after a cancelled pass.
    The gate declined, the UI warning was deduped away, and no log line ever named it."""
    agent, messages = _agent(gate=(False, "cooldown:412")), _messages()
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        _gate(agent, messages)
    records = _declines(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert "blocked:cooldown:412" in records[0].getMessage()


def test_one_line_per_turn_however_many_tool_iterations(caplog):
    """A 300-tool-iteration turn must not emit 300 lines."""
    agent, messages = _agent(enabled=False), _messages()
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        for _ in range(25):
            _gate(agent, messages)
    assert len(_declines(caplog)) == 1
    agent._user_turn_count += 1  # a new user turn is a new line
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        _gate(agent, messages)
    assert len(_declines(caplog)) == 2


def test_below_the_trigger_stays_quiet(caplog):
    """Nothing is wrong under the threshold: no line, no log noise."""
    agent, messages = _agent(enabled=False), _messages()
    agent.context_compressor.last_prompt_tokens = THRESHOLD - 1
    with caplog.at_level(logging.DEBUG, logger="agent.conversation_loop"):
        _gate(agent, messages)
    assert _declines(caplog) == []


def test_the_tripped_anti_thrash_breaker_names_itself(caplog):
    """The incident's most likely suppressor, at the real seam.

    Two compactions that leave the provider's real count at/over the threshold trip the
    anti-thrash breaker (``_apply_real_prompt_verdict`` -> ``_tripped``), after which every
    automatic pass declines with reason ``ineffective`` for the life of the process — the state
    chk-n5va.98 measured is exactly in-memory and dies with the agent, while state.db stays clean.
    Pre-fix the only trace was a UI warning deduped per block kind, so nothing reached a log.
    """
    from agent.context_compressor import ContextCompressor

    compressor = ContextCompressor(model="m", config_context_length=200_000)
    compressor._verify_compaction_cleared_threshold = True
    for _ in range(2):  # two passes that did not clear the line
        compressor.last_prompt_tokens = compressor.threshold_tokens
        compressor._apply_real_prompt_verdict()
    assert compressor.should_compress_info(compressor.threshold_tokens) == (False, "ineffective")

    agent, messages = _agent(), _messages()
    agent.context_compressor = compressor
    agent._user_turn_count = 9
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        _gate(agent, messages)
    records = _declines(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert "blocked:ineffective" in records[0].getMessage()
