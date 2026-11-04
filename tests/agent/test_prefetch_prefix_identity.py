"""Prefetch adoption across a content-only prune of the armed prefix.

Between arming and the gate the main thread may commit a proactive tool-result
prune (``turn_preflight``): ``messages`` is rebound to a NEW list of NEW dicts
whose old tool bodies are replaced by summary stubs. The prefetch snapshot still
holds the undemoted rows, so a byte-identical prefix comparison reports
``prefix_diverged`` and throws away a summary the worker already paid for and
committed to the SessionDB — and the blocking path then pays for a second full
compaction of the same history (two ~300k-token summary calls, 51s apart).

The prefix check therefore compares message IDENTITY (role / tool_call_id /
non-demotable content), not raw tool bodies: the summary replaces the whole
prefix, so a prune-demoted body changes nothing about what the summary says,
while a rewind, an edit-resend or a competing compaction still change identity.
"""

from __future__ import annotations

from types import SimpleNamespace

import agent.turn_prefetch_compaction as tac
from agent.context_compressor import _DB_PERSISTED_MARKER, _summarize_tool_result


def _snapshot(n_rounds: int = 6) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "sys"}]
    for i in range(n_rounds):
        cid = f"call-{i}"
        msgs.append({
            "role": "assistant",
            "content": f"assistant-{i}",
            "tool_calls": [{
                "id": cid, "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "f.py"}'},
            }],
        })
        msgs.append({"role": "tool", "tool_call_id": cid, "content": "BODY-" + ("x" * 4000)})
    for m in msgs:
        m[_DB_PERSISTED_MARKER] = "db-persisted"
    return msgs


def _result() -> tuple[list[dict], str]:
    return (
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "[CONTEXT COMPACTION] summary of the prefix"},
        ],
        "system-prompt",
    )


def _prune_tool_bodies(msgs: list[dict]) -> list[dict]:
    """What the main-thread proactive prune commits: new rows, demoted bodies."""
    out = []
    for m in msgs:
        row = dict(m)
        if row.get("role") == "tool":
            row["content"] = _summarize_tool_result(
                "read_file", '{"path": "f.py"}', row["content"]
            )
        out.append(row)
    return out


def _state(snap: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(snapshot=snap, arm_index=len(snap), result=None)


def test_adoption_survives_pruned_tool_bodies():
    snap = _snapshot()
    state = _state(snap)
    live = _prune_tool_bodies(snap) + [{"role": "user", "content": "after the prune"}]

    new_messages, adopted, reason = tac.adopt_prefetch_result(live, state, _result())

    assert adopted, f"pruned prefix discarded the paid-for summary ({reason})"
    assert reason is None
    assert new_messages == _result()[0] + [{"role": "user", "content": "after the prune"}]


def test_adoption_still_discards_an_edit_resend():
    snap = _snapshot()
    state = _state(snap)
    live = [dict(m) for m in snap]
    live[3] = {**live[3], "content": "edited user-facing answer"}

    _, adopted, reason = tac.adopt_prefetch_result(live, state, _result())

    assert not adopted and reason == "prefix_diverged"


def test_adoption_still_discards_a_rewind():
    snap = _snapshot()
    state = _state(snap)
    live = [dict(m) for m in snap]
    del live[5]

    _, adopted, reason = tac.adopt_prefetch_result(live, state, _result())

    assert not adopted and reason == "prefix_diverged"


def test_adoption_still_discards_a_competing_compaction():
    snap = _snapshot()
    state = _state(snap)
    # Another pass already replaced the head with a different summary row.
    live = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[CONTEXT COMPACTION] a DIFFERENT summary"},
    ] + snap[len(snap):]

    _, adopted, reason = tac.adopt_prefetch_result(live, state, _result())

    assert not adopted and reason == "prefix_diverged"
