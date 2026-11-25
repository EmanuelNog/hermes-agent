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


def test_a_copied_list_is_stripped_and_a_quoted_heading_survives(home):
    """The summarizer may echo the previous handoff: its block is stripped, its prose is not."""
    block = f"\n\n{trs.ARCHIVE_LIST_HEADING}\n- ~/.hermes/cache/pruned/a.txt\n- ~/.hermes/cache/pruned/b.json"
    quoted = f'## Note\nUser asked: "why does {trs.ARCHIVE_LIST_HEADING} grow?"'
    assert trs.strip_archive_list(quoted + block + block) == quoted

    one_index = f"\n\n{trs.ARCHIVE_LIST_HEADING}\n- ~/.hermes/cache/pruned/local-abc.index.jsonl"
    assert trs.strip_archive_list("## Goal\nread them\n" + one_index) == "## Goal\nread them"


def test_a_corrupt_index_row_costs_that_row_not_the_reference_set(home):
    d = home / "cache" / "pruned"
    d.mkdir(parents=True)
    (d / "s1.index.jsonl").write_text("\n".join([
        json.dumps({"path": "~/.hermes/cache/pruned/a.json"}),
        "{not json",
        json.dumps({"path": "../../etc/passwd"}),
        json.dumps({"path": "~/.hermes/cache/pruned/gone.json"}),
        "",
    ]), encoding="utf-8")

    refs = trs._expand_index_refs({"cache/pruned/s1.index.jsonl"}, home)
    names = {ref.removeprefix("cache/pruned/") for ref in refs} - {"s1.index.jsonl"}
    assert "a.json" in names                       # the readable row still protects its archive
    assert "gone.json" in names                    # a dangling name is harmless to the sweep
    assert all("/" not in name and ".." not in name for name in names)  # a path-shaped row cannot escape


def test_the_same_body_archived_twice_is_two_rows_not_one(home):
    msgs = [{"role": "assistant", "content": "", "tool_calls": [{
        "id": "call_a", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "y" * 20_000}]
    pruned = [msgs[0], {"role": "tool", "tool_call_id": "call_a", "content": "[tool] short"}]

    trs.archive_pruned_originals(msgs, pruned, "s1")
    trs.archive_pruned_originals(msgs, pruned, "s1")

    rows = [json.loads(line) for line in
            (home / "cache" / "pruned" / "s1.index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and len({r["sha256"] for r in rows}) == 1  # one row per archive written, no dedupe


def test_a_session_that_cannot_name_a_file_writes_no_index_and_keeps_the_flat_list(home):
    msgs = [{"role": "assistant", "content": "", "tool_calls": [{
        "id": "call_a", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "z" * 20_000}]
    pruned = [msgs[0], {"role": "tool", "tool_call_id": "call_a", "content": "[tool] short"}]

    for sid in ("", "a/b"):
        rows, written, reverted = trs.archive_pruned_originals(msgs, pruned, sid)
        assert written and not reverted
        assert not list((home / "cache" / "pruned").glob("*.index.jsonl"))
        block = trs.archive_list_for_summary(trs.pruned_archive_refs(rows), [], sid)
        assert block.count("\n- ") == 1  # the flat fallback still names the archive


def test_a_publishing_backend_is_named_at_the_path_its_read_file_opens(home, monkeypatch):
    """The index is written beside the archive on the host and named in the sandbox's own path form."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    msgs = [{"role": "assistant", "content": "", "tool_calls": [{
        "id": "call_a", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "w" * 20_000}]
    pruned = [msgs[0], {"role": "tool", "tool_call_id": "call_a", "content": "[tool] short"}]

    rows, written, _ = trs.archive_pruned_originals(msgs, pruned, "s1")
    assert written
    block = trs.archive_list_for_summary(trs.pruned_archive_refs(rows), [], "s1")
    assert block.count("\n- ") == 1
    assert "/.hermes/cache/pruned/s1.index.jsonl" in block  # the path the sandbox's read_file opens
