"""MANUAL-ONLY (owner decision 2026-09-23): this prefetchbig harness is NOT part of automated fork validation (cost). Do not wire into the sync pipeline/CI. Run only when the owner explicitly asks for big-scale testing. Automated E2E uses prefetchdev only — see scripts/prefetch_e2e/README.md.
"""
import os, sys
os.environ.setdefault("HERMES_HOME", "/home/agentuser/.hermes/profiles/prefetchbig")
sys.path.insert(0, "/home/agentuser/Projects/hermes-prefetch-compress")
from hermes_state import SessionDB
from pathlib import Path
db = SessionDB(db_path=Path("/home/agentuser/.hermes/profiles/prefetchbig/state.db"))
SID = "20260907_230521_d588b3"
msgs = []
tool_idx = 200
for b in range(29, 55):
    lines = []
    for j in range(220):
        lines.append(
            f"audit[{b:02d}:{j:03d}] path=/var/log/svc-{b % 7}-{j % 5}.log ts=2026-09-0{(j % 9)+1}T"
            f"{j // 60:02d}:{(j % 60):02d}Z level={'INFO' if j % 3 else 'WARN'} "
            f"msg=request #{2000 + b * 900 + j} sid=shopper-id-7f3a9 elapsed_ms={40+(j*53)%5000} "
            f"status={200 if j % 4 else 500} body_len={900+(j*777)%52000}"
        )
    tool_idx += 1
    call_id = f"call_audit2_{tool_idx}"
    content = (f"--- audit dump {b}/55 (verbatim) ---\n" + "\n".join(lines)
               + f"\n--- end audit dump {b}/55 ---\np50=1670ms p95=5590ms errors=3 cache=0.91 no anomaly")
    msgs.append({"role": "assistant", "tool_calls": [{"id": call_id, "type": "function",
                  "function": {"name": "terminal", "arguments": "{}"}}], "content": None})
    msgs.append({"role": "tool", "tool_call_id": call_id, "name": "terminal", "content": content})
    msgs.append({"role": "assistant", "content": (
        f"Batch {b}/55 captured verbatim; waiting for the full set before the consolidated analysis. "
        "No decisions until every batch lands — partial data biases the anomaly detector."
    )})
db.append_messages_batch(SID, msgs, chunk_rows=500)
total = sum(len(m.get("content") or "") for m in msgs)
n, c = db.execute("select count(*), coalesce(sum(length(content)),0) from messages where session_id=?", (SID,)).fetchone()
print(f"appended {len(msgs)} rows ({total} chars); session now: rows={n} chars={c} ~real={int(c/5)}")
