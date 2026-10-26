"""An UNATTENDED run must not sit out a long provider quota window (chk-n5va.15).

On 2026-10-25 pc2's `hermes -z` one-shots produced no output for 3+ minutes and looked like a
hang, because the conversation loop honours the provider's ``Retry-After`` on a 429 - up to
``RETRY_AFTER_CAP_S`` (600 s) - and retries. A person watching a TUI can decide to sit it out; a
one-shot has nobody there, so the silence IS the failure mode: unattended callers (cron, `hermes -z`,
`chat -q`) read it as a hang and their logs carry no verdict.

The cap is deliberately applied only to runs ``is_attended`` calls unattended: a paid bucket reopens
on time and ``fallback.min_switch_reset_seconds`` relies on an attended session waiting it out.
"""

import logging
from unittest.mock import MagicMock

import pytest

from agent.retry_utils import LIVE_RETRY_WAIT_CAP_S, RETRY_AFTER_CAP_S
from agent.turn_recovery import compute_error_backoff


def _rate_limited(retry_after: str = "600") -> Exception:
    err = Exception("HTTP 429: rate limit exceeded")
    err.response = MagicMock(headers={"retry-after": retry_after})
    return err


def _agent(platform: str = "cli"):
    agent = MagicMock()
    agent.platform = platform
    from agent.status_output import StatusOutputMixin
    for name in ("_emit_diagnostic_wait", "_buffer_diagnostic_status"):
        setattr(agent, name, getattr(StatusOutputMixin, name).__get__(agent))
    agent._client_log_context.return_value = ""
    return agent


def _wait(agent, err) -> float:
    return compute_error_backoff(
        agent, err, retry_count=1, max_retries=3, is_rate_limited=True,
        is_zai_coding_overload=False, base_url="https://example.test/v1", model="test/model",
    )


def _one_shot_env(monkeypatch):
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_SOURCE_EXPLICIT", raising=False)


def _attended_env(monkeypatch):
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "cli")
    monkeypatch.setenv("HERMES_SESSION_SOURCE_EXPLICIT", "1")


def test_the_one_shot_really_is_classified_unattended(monkeypatch):
    """The wiring, not just the cap: `hermes -z` runs under ONESHOT_SOURCE, which is not attended."""
    from agent.session_source import is_attended
    _one_shot_env(monkeypatch)
    assert is_attended(_agent("cli")) is False


def test_a_oneshot_does_not_sit_out_a_ten_minute_quota_window(monkeypatch):
    """The reported symptom: retry attempts at 600.0s each, so a dead pool meant 10+ minutes."""
    _one_shot_env(monkeypatch)
    wait = _wait(_agent("cli"), _rate_limited("600"))
    assert wait <= LIVE_RETRY_WAIT_CAP_S, (
        f"an unattended one-shot must not sleep through the provider's whole quota window: {wait}s")


def test_a_long_retry_after_is_still_capped_at_the_hard_ceiling(monkeypatch):
    """The pathological-value guard stays: a 9000 s Retry-After never becomes the wait."""
    _one_shot_env(monkeypatch)
    assert _wait(_agent("cli"), _rate_limited("9000")) <= LIVE_RETRY_WAIT_CAP_S


def test_an_attended_run_still_waits_out_the_provider_window(monkeypatch):
    """Do not spend the fix on attended sessions: a paid bucket reopens on time, and the fallback
    switch logic depends on the wait actually happening."""
    _attended_env(monkeypatch)
    wait = _wait(_agent("cli"), _rate_limited("600"))
    assert wait == pytest.approx(RETRY_AFTER_CAP_S), (
        "an attended session must still honour the provider's Retry-After "
        f"(capped at {RETRY_AFTER_CAP_S}s): {wait}s")


def test_the_truncated_wait_is_reported_not_silent(monkeypatch, caplog):
    """Whatever the wait shrinks to, the log must say it was shortened - and by which policy."""
    _one_shot_env(monkeypatch)
    with caplog.at_level(logging.WARNING):
        _wait(_agent("cli"), _rate_limited("600"))
    assert "unattended_quota_cap" in caplog.text, caplog.text
