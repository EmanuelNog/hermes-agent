"""The session archive index: one JSON row per archived result, named by ONE summary line.

Why this exists (measured 2026-11-25): the flat appendix was 491 KB of 522 KB in a summary, and a
path in it carried only tool + archive-batch timestamp — no call id, no args, no turn. The index
keeps the same recovery promise at ~1 line and adds the join back to the call.
"""
import json

import pytest

from tools import tool_result_storage as trs


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_index_row_names_the_file_its_tool_and_its_call(home):
    d = home / "cache" / "pruned"
    d.mkdir(parents=True)
    host = d / "local-abc_20260101T000000Z_terminal_deadbeef.json"
    host.write_text("{}", encoding="utf-8")

    assert trs.append_archive_index_row(str(host), {
        "session": "local-abc", "path": "~/.hermes/cache/pruned/" + host.name,
        "tool": "terminal", "args_preview": '{"command": "ls"}', "call_id": "call_1",
        "ts": 1.0, "chars": 2, "sha256": "x" * 64})

    row = json.loads((d / "local-abc.index.jsonl").read_text(encoding="utf-8").strip())
    assert row["tool"] == "terminal" and row["call_id"] == "call_1"
    assert row["path"].endswith(host.name)


def test_summary_names_the_index_instead_of_every_archive(home):
    d = home / "cache" / "pruned"
    d.mkdir(parents=True)
    (d / "local-abc.index.jsonl").write_text("{}\n", encoding="utf-8")
    named = {"cache/pruned/a.json", "cache/pruned/b.json"}

    one = trs.archive_list_for_summary(named, [], session_id="local-abc")
    assert one.count("\n- ") == 1
    assert "local-abc.index.jsonl" in one

    # No index (older session, backend that cannot write it): the flat list is the fallback.
    flat = trs.archive_list_for_summary(named, [], session_id="")
    assert flat.count("\n- ") == 2


def test_naming_the_index_keeps_every_archive_it_lists(home):
    d = home / "cache" / "pruned"
    d.mkdir(parents=True)
    for name in ("a.json", "b.json"):
        (d / name).write_text("{}", encoding="utf-8")
    (d / "local-abc.index.jsonl").write_text("\n".join(
        json.dumps({"path": f"~/.hermes/cache/pruned/{name}"}) for name in ("a.json", "b.json")
    ), encoding="utf-8")

    out = trs._expand_index_refs({"cache/pruned/local-abc.index.jsonl"}, home)
    assert out == {"cache/pruned/local-abc.index.jsonl",
                   "cache/pruned/a.json", "cache/pruned/b.json"}


def test_pruning_writes_an_index_row_per_archived_result(home):
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "terminal", "arguments": '{"command": "ls"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "x" * 20_000},
    ]
    pruned = [dict(msgs[0]), {"role": "tool", "tool_call_id": "call_1", "content": "[tool] short"}]

    rows, written, reverted = trs.archive_pruned_originals(msgs, pruned, "local-abc")

    assert written and reverted == 0
    assert "full result: " in rows[1]["content"]
    index = home / "cache" / "pruned" / "local-abc.index.jsonl"
    entries = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1
    assert entries[0]["call_id"] == "call_1" and entries[0]["tool"] == "terminal"
    assert entries[0]["chars"] == 20_000
    assert entries[0]["args_preview"] == '{"command": "ls"}'
