"""Content search over the messages FTS index, exposed by the API server as GET /api/search.

Why this exists: `/api/sessions?title=` resolves through `_chain_search_where`, which matches session
titles and ids only. An API client (an orchestrating agent, for instance) therefore cannot look up what
was *said* in a transcript unless it pulls whole sessions and reads them itself. This module answers
that with the same index `session_search` uses.

Read-only by construction: one SELECT, no writes, no session creation.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Sequence

MAX_LIMIT = 100
DEFAULT_LIMIT = 20
SNIPPET_TOKENS = 24
DEFAULT_ROLES: tuple[str, ...] = ("user", "assistant")


def _redact(text: str) -> str:
    """Redact at the boundary — a snippet leaves the process and may be quoted by another agent."""
    if not text:
        return ""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:  # redaction must never break a read
        return text


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _roles(roles: Sequence[str] | Iterable[str] | None) -> tuple[str, ...]:
    if roles is None:
        return DEFAULT_ROLES
    cleaned = tuple(r.strip() for r in roles if isinstance(r, str) and r.strip())
    return cleaned or DEFAULT_ROLES


def search_messages(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int | None = DEFAULT_LIMIT,
    session_id: str | None = None,
    roles: Sequence[str] | Iterable[str] | None = None,
) -> list[dict]:
    """Return hits as dicts: session_id, message_id, role, timestamp, snippet, session_title.

    Raises ValueError for an empty query or an FTS5 syntax error, so the handler can answer 400
    instead of 500 — a malformed query is the caller's mistake, not the server's.
    """
    text = (query or "").strip()
    if not text:
        raise ValueError("query is required")

    role_list = _roles(roles)
    placeholders = ",".join("?" for _ in role_list)
    sql = f"""
        SELECT m.id          AS message_id,
               m.session_id  AS session_id,
               m.role        AS role,
               m.timestamp   AS timestamp,
               snippet(messages_fts, 0, '', '', '…', {SNIPPET_TOKENS}) AS snippet,
               (SELECT s.title FROM sessions s WHERE s.id = m.session_id) AS session_title
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        WHERE messages_fts MATCH ?
          AND m.role IN ({placeholders})
    """
    params: list[object] = [text, *role_list]
    if session_id and session_id != "*":
        sql += " AND m.session_id = ?"
        params.append(session_id)
    sql += " ORDER BY m.timestamp DESC, m.id DESC LIMIT ?"
    params.append(_clamp_limit(limit))

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as err:
        message = str(err)
        lowered = message.lower()
        if "fts5" in lowered or "syntax error" in lowered:
            raise ValueError(f"fts5: {message}") from err
        if "no such table" in lowered:
            raise ValueError(f"fts5: search index unavailable ({message})") from err
        raise

    hits: list[dict] = []
    for message_id, sess, role, timestamp, snip, title in rows:
        hits.append({
            "session_id": sess,
            "message_id": message_id,
            "role": role,
            "timestamp": timestamp,
            "snippet": _redact(snip or ""),
            "session_title": title,
        })
    return hits
