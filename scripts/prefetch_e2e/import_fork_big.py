"""MANUAL-ONLY (owner decision 2026-09-23): this prefetchbig harness is NOT part of automated fork validation (cost). Do not wire into the sync pipeline/CI. Run only when the owner explicitly asks for big-scale testing. Automated E2E uses prefetchdev only — see scripts/prefetch_e2e/README.md.
"""
#!/usr/bin/env python3
"""Import a fork transcript from the MAIN state.db (read-only) into the
prefetchbig dev profile home, for big-scale prefetch validation (bead .5).
Main install is never written to; the constraint is main stays untouched.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/home/agentuser/Projects/hermes-prefetch-compress")

from hermes_state import SessionDB

MAIN_DB = Path("/home/agentuser/.hermes/state.db")
BIG_DB = Path("/home/agentuser/.hermes/profiles/prefetchbig/state.db")
FORKS = [
    "20260907_230521_d588b3",  # default reasoning
    "20260907_230521_5a165c",  # low
    "20260907_230521_aa5684",  # none
]

src = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
for sid in FORKS:
    rows = src.execute(
        "select role, content, tool_call_id, tool_name, tool_calls from messages "
        "where session_id=? and active=0 order by id",
        (sid,),
    ).fetchall()
    if not rows:
        print(f"{sid}: NO ROWS in main db")
        continue
    dst = SessionDB(db_path=BIG_DB)
    dst.ensure_session(session_id=sid, source="cli", model="deepseek-v4-flash")
    msgs = []
    for role, content, tcid, tname, tcalls in rows:
        m = {"role": role, "content": content}
        if role == "tool":
            m["tool_call_id"] = tcid or ""
            if tname:
                m["name"] = tname
        if tcalls:
            try:
                m["tool_calls"] = json.loads(tcalls)
            except Exception:
                pass
        msgs.append(m)
    dst.append_messages_batch(sid, msgs, chunk_rows=500)
    dst.set_session_title(sid, f"big-fork import {sid}")
    chars = sum(len(str(m.get("content") or "")) for m in msgs)
    print(f"{sid}: imported {len(msgs)} rows, {chars} chars (~{chars//4} tokens)")
src.close()