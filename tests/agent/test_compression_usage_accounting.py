"""A compaction's summary spend always lands on the per-task ledger (#chk-n5va.87).

The aux chokepoint (``agent/aux_accounting.record_aux_usage``) only records when a session
handle is published, and only a turn published one. Every compaction driven from outside a
turn — ``/compress`` through ``session.mutate``, gateway hygiene on a temporary agent, a
micro pass on a worker thread — summarized for real and recorded nothing, so cost reviews
trusting ``task='compression'`` under-reported summarisation.

These exercise the production chain (resolve → client.create → validate → record) with only
the HTTP client faked; ``_validate_llm_response``/``record_aux_usage`` stay real.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.aux_accounting import reset_accounting_context, set_accounting_context
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(Path(tmp_path) / "state.db")


def _response(content="SUMMARY " * 40, model="summary-model", prompt=100, completion=20):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                              total_tokens=prompt + completion),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
    )


def _client(response):
    return SimpleNamespace(
        base_url="http://127.0.0.1:9/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)),
    )


def _compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(
            model="main-model", provider="main-provider", quiet_mode=True,
            base_url="http://127.0.0.1:9/v1", api_key="test-key",
        )


def _aux_route(model="summary-model"):
    """Pin the aux route to a fake client without touching the accounting chokepoint."""
    return (
        patch("agent.auxiliary_client._resolve_task_provider_model",
              return_value=("custom", model, None, None, None)),
        patch("agent.auxiliary_client._get_cached_client", return_value=(_client(_response()), model)),
        patch("agent.auxiliary_client._get_auxiliary_task_config", return_value={}),
    )


def _compression_rows(db, session_id=None):
    cursor = db._conn.execute(
        "SELECT session_id, model, task, api_call_count, input_tokens, output_tokens"
        " FROM session_model_usage WHERE task='compression'")
    names = [col[0] for col in cursor.description]
    rows = [dict(zip(names, row)) for row in cursor.fetchall()]
    return rows if session_id is None else [r for r in rows if r["session_id"] == session_id]


def test_out_of_turn_summary_records_compression_usage(db):
    """No ambient context (a mutation/hygiene worker): the compressor's bound session is used."""
    db.create_session("s1", source="cli")
    compressor = _compressor()
    compressor.bind_session_state(db, "s1")
    resolve, cached, cfg = _aux_route()
    with resolve, cached, cfg:
        out = compressor._call_summary_llm("summarize this", 0.0)

    assert out and "SUMMARY" in out
    rows = _compression_rows(db, "s1")
    assert rows, "the summary call spent tokens but no task='compression' row was recorded"
    row = rows[0]
    assert (row["model"], row["task"], row["api_call_count"]) == ("summary-model", "compression", 1)
    assert (row["input_tokens"], row["output_tokens"]) == (100, 20)


def test_out_of_turn_summary_on_main_model_fallback_records_compression_usage(db):
    """The aux-fallback-to-main path is the one the ledger used to lose entirely."""
    db.create_session("s1", source="cli")
    compressor = _compressor()
    compressor.bind_session_state(db, "s1")
    compressor._fallback_to_main_for_compression(RuntimeError("aux quota exhausted"), "failed",
                                                failed_model="summary-model")
    resolve, cached, cfg = _aux_route()
    with resolve, cached, cfg:
        out = compressor._call_summary_llm("summarize this", 0.0)

    assert out
    rows = _compression_rows(db, "s1")
    assert rows, "a main-model summary landed no task='compression' row"
    assert rows[0]["model"] == "summary-model"
    # The route choice is still visible in telemetry, so the row is not mistaken for aux spend.
    assert compressor._compression_method() == "aux_fallback_main"


def test_micro_compaction_records_compression_usage(db):
    """Per-turn micro-summarization runs on the same aux chokepoint and owes the same row."""
    db.create_session("s1", source="cli")
    compressor = _compressor()
    compressor.bind_session_state(db, "s1")
    resolve, cached, cfg = _aux_route()
    with resolve, cached, cfg:
        merged = compressor._micro_summarize_one("user: hi\nassistant: hello")

    assert merged
    rows = _compression_rows(db, "s1")
    assert rows, "a micro summary spent tokens but recorded no task='compression' row"
    assert rows[0]["api_call_count"] == 1


def test_turn_context_wins_over_the_compressor_binding(db):
    """Inside a turn the spend bills to the turn's session (compression mints child ids)."""
    db.create_session("launch", source="cli")
    db.create_session("s1", source="cli")
    compressor = _compressor()
    compressor.bind_session_state(db, "s1")
    token = set_accounting_context(db, "launch")
    try:
        resolve, cached, cfg = _aux_route()
        with resolve, cached, cfg:
            assert compressor._call_summary_llm("summarize this", 0.0)
    finally:
        reset_accounting_context(token)

    assert [r["session_id"] for r in _compression_rows(db)] == ["launch"]
