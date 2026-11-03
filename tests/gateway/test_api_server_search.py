"""GET /api/search — content search over the messages FTS index (TDD, tests first).

The route exists because `/api/sessions?title=` matches session titles and ids only
(`_chain_search_where`), which leaves an API client unable to look up message content — the thing a
bridging agent needs most.

The query lives in `gateway/platforms/api_server_search.py` so it can be tested without booting the
HTTP server; the handler is a thin adapter over it.
"""

import sqlite3

import pytest

from gateway.platforms.api_server_search import search_messages


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, content='messages', content_rowid='id');
        """
    )
    rows = [
        (1, "sess-a", "user", "the egress proxy lives on port 3137", 1000.0),
        (2, "sess-a", "assistant", "confirmed: egress-iron listens on 3137/tcp", 1001.0),
        (3, "sess-b", "user", "unrelated note about the printer", 1002.0),
        (4, "sess-b", "assistant", "set API_SERVER_KEY=sk-live-abcdef0123456789 on the box", 1003.0),
    ]
    conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO sessions VALUES (?,?)",
                     [("sess-a", "Egress proxy work"), ("sess-b", "Printer setup")])
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    conn.commit()
    return conn


def test_hit_shape_carries_the_fields_a_client_needs(db):
    hits = search_messages(db, "egress")
    assert hits, "expected at least one hit"
    first = hits[0]
    assert set(first) == {"session_id", "message_id", "role", "timestamp", "snippet", "session_title"}
    assert first["session_id"] == "sess-a"
    assert first["session_title"] == "Egress proxy work"
    assert isinstance(first["message_id"], int)
    assert "egress" in first["snippet"].lower()


def test_role_filter(db):
    only_user = search_messages(db, "egress", roles=("user",))
    assert {h["role"] for h in only_user} == {"user"}
    both = search_messages(db, "egress", roles=("user", "assistant"))
    assert {h["role"] for h in both} == {"user", "assistant"}


def test_session_filter(db):
    hits = search_messages(db, "egress", session_id="sess-a")
    assert hits and all(h["session_id"] == "sess-a" for h in hits)
    assert search_messages(db, "egress", session_id="sess-b") == []


def test_limit_is_clamped_not_trusted(db):
    assert len(search_messages(db, "egress", limit=1)) == 1
    # over the cap clamps to 100 rather than raising; under 1 clamps to 1
    assert len(search_messages(db, "egress", limit=10_000)) <= 100
    assert len(search_messages(db, "egress", limit=0)) == 1


def test_bad_fts_syntax_is_a_valuable_error_not_a_crash(db):
    with pytest.raises(ValueError) as err:
        search_messages(db, "AND AND")
    assert "fts5" in str(err.value).lower()


def test_empty_query_rejected(db):
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            search_messages(db, bad)


def test_no_hits_is_an_empty_list(db):
    assert search_messages(db, "nonexistentzzz") == []


def test_snippets_never_leak_secrets(db):
    hits = search_messages(db, "API_SERVER_KEY")
    assert hits, "the seeded secret row must be found by its own text"
    for h in hits:
        assert "sk-live-abcdef0123456789" not in h["snippet"], "a snippet must be redacted"


def test_results_are_newest_first(db):
    hits = search_messages(db, "egress")
    stamps = [h["timestamp"] for h in hits]
    assert stamps == sorted(stamps, reverse=True)
