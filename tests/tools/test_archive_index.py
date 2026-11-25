"""The session archive index: one JSON row per archived result, named by ONE summary line.

Why this exists (measured 2026-11-25): the flat appendix was 491 KB of a 522 KB summary, and a path in it
carried only tool + archive-batch timestamp — no call id, no args, no turn. The index keeps the same
recovery promise at ~1 line and adds the join back to the call.
"""
import json

import pytest

from tools import tool_result_storage as trs


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    return tmp_path


def _round(call_id="call_1", body="x" * 20_000, tool="terminal"):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": tool, "arguments": '{"command": "ls"}'}}]},
        {"role": "tool", "tool_call_id": call_id, "content": body},
    ]


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
    assert set(row) == {"session", "path", "tool", "args_preview", "call_id", "ts", "chars", "sha256"}
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
    msgs = _round()
    pruned = [msgs[0], {"role": "tool", "tool_call_id": "call_1", "content": "[tool] short"}]

    rows, written, reverted = trs.archive_pruned_originals(msgs, pruned, "local-abc")

    assert written and reverted == 0
    assert "full result: " in rows[1]["content"]
    entries = [json.loads(line) for line in
               (home / "cache" / "pruned" / "local-abc.index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1
    assert entries[0]["call_id"] == "call_1" and entries[0]["tool"] == "terminal"
    assert entries[0]["chars"] == 20_000
    assert entries[0]["args_preview"] == '{"command": "ls"}'


def test_a_failed_index_write_keeps_the_archive_and_falls_back_to_the_flat_list(home, monkeypatch):
    """A failed index write must never revert a prune: the archive and its stub stand, the summary lists it."""
    monkeypatch.setattr(trs, "append_archive_index_row", lambda *a, **k: False)
    msgs = _round()
    pruned = [msgs[0], {"role": "tool", "tool_call_id": "call_1", "content": "[tool] short"}]

    rows, written, reverted = trs.archive_pruned_originals(msgs, pruned, "local-abc")

    assert written and reverted == 0
    assert "full result: " in rows[1]["content"]
    assert not list((home / "cache" / "pruned").glob("*.index.jsonl"))
    block = trs.archive_list_for_summary(trs.pruned_archive_refs(rows), [], "local-abc")
    assert block.count("\n- ") == 1 and block.rstrip().endswith(".txt")


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
    msgs = _round()
    pruned = [msgs[0], {"role": "tool", "tool_call_id": "call_1", "content": "[tool] short"}]

    trs.archive_pruned_originals(msgs, pruned, "s1")
    trs.archive_pruned_originals(msgs, pruned, "s1")

    rows = [json.loads(line) for line in
            (home / "cache" / "pruned" / "s1.index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and len({r["sha256"] for r in rows}) == 1  # one row per archive written, no dedupe


def test_a_session_that_cannot_name_a_file_writes_no_index_and_keeps_the_flat_list(home):
    msgs = _round()
    pruned = [msgs[0], {"role": "tool", "tool_call_id": "call_1", "content": "[tool] short"}]

    for sid in ("", "a/b"):
        rows, written, reverted = trs.archive_pruned_originals(msgs, pruned, sid)
        assert written and not reverted
        assert not list((home / "cache" / "pruned").glob("*.index.jsonl"))
        block = trs.archive_list_for_summary(trs.pruned_archive_refs(rows), [], sid)
        assert block.count("\n- ") == 1  # the flat fallback still names the archive


def test_a_missing_index_is_not_offered_as_a_recovery_path(home):
    """With the index file gone, the flat fallback names the archives, never the dead index path."""
    block = trs.archive_list_for_summary(
        {"cache/pruned/a.json", "cache/pruned/s1.index.jsonl"}, [], "s1")
    lines = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(lines) == 1 and lines[0].endswith("/a.json")


def test_a_publishing_backend_is_named_at_the_path_its_read_file_opens(home, monkeypatch):
    """The index is written beside the archive on the host and named in the sandbox's own path form."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    msgs = _round(body="w" * 20_000)
    pruned = [msgs[0], {"role": "tool", "tool_call_id": "call_1", "content": "[tool] short"}]

    rows, written, _ = trs.archive_pruned_originals(msgs, pruned, "s1")
    assert written
    block = trs.archive_list_for_summary(trs.pruned_archive_refs(rows), [], "s1")
    assert block.count("\n- ") == 1
    assert "/.hermes/cache/pruned/s1.index.jsonl" in block  # the path the sandbox's read_file opens


def test_the_sweep_keeps_index_listed_archives_and_drops_the_ones_a_broken_row_hid(home):
    """The retention answer for a real state.db: readable rows keep their archive, broken rows lose it."""
    import os

    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_messages, seed_session

    d = home / "cache" / "pruned"
    d.mkdir(parents=True)
    for name in ("a.json", "b.json", "c.json"):
        (d / name).write_text("{}", encoding="utf-8")
    (d / "s1.index.jsonl").write_text("\n".join([
        json.dumps({"path": "~/.hermes/cache/pruned/a.json"}),
        "{this row is broken but b.json is its archive",
    ]) + "\n", encoding="utf-8")

    summary = f"## Goal\nread\n\n{trs.ARCHIVE_LIST_HEADING}\n- ~/.hermes/cache/pruned/s1.index.jsonl"
    db = open_persistence(profile_scope(home=home), eager=True)
    try:
        seed_session(db, "s1", "cli")
        seed_messages(db, "s1", {"role": "assistant", "content": summary})
    finally:
        db.close()

    old = 1_000_000_000
    for p in d.iterdir():
        os.utime(p, (old, old))
    assert trs.cleanup_spillover_cache(max_age_hours=1) == 2   # b.json (broken row) and c.json (no row)
    assert sorted(p.name for p in d.iterdir()) == ["a.json", "s1.index.jsonl"]


def test_one_sessions_index_does_not_keep_another_sessions_archives(home):
    import os

    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_messages, seed_session

    d = home / "cache" / "pruned"
    d.mkdir(parents=True)
    for name in ("s1a.json", "s2a.json"):
        (d / name).write_text("{}", encoding="utf-8")
    for sid, archive in (("s1", "s1a.json"), ("s2", "s2a.json")):
        (d / f"{sid}.index.jsonl").write_text(
            json.dumps({"path": f"~/.hermes/cache/pruned/{archive}"}) + "\n", encoding="utf-8")

    summary = f"## Goal\nread\n\n{trs.ARCHIVE_LIST_HEADING}\n- ~/.hermes/cache/pruned/s1.index.jsonl"
    db = open_persistence(profile_scope(home=home), eager=True)
    try:
        seed_session(db, "s1", "cli")
        seed_messages(db, "s1", {"role": "assistant", "content": summary})
    finally:
        db.close()

    old = 1_000_000_000
    for p in d.iterdir():
        os.utime(p, (old, old))
    assert trs.cleanup_spillover_cache(max_age_hours=1) == 2   # s2a.json + s2.index.jsonl
    assert sorted(p.name for p in d.iterdir()) == ["s1.index.jsonl", "s1a.json"]
