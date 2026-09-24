"""MANUAL-ONLY (owner decision 2026-09-23): this prefetchbig harness is NOT part of automated fork validation (cost). Do not wire into the sync pipeline/CI. Run only when the owner explicitly asks for big-scale testing. Automated E2E uses prefetchdev only — see scripts/prefetch_e2e/README.md.
"""
#!/usr/bin/env python3
"""Inflate session 20260907_230521_d588b3 in the prefetchbig home with realistic
audit-log filler so the REAL request lands ~620-640K tokens (arm 600K, threshold
650K). Empirical ratio from API #1: real_in ~= 0.323*chars + 99K.
Current: 628K chars -> ~302K real. Need ~+1.0M chars.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("HERMES_HOME", "/home/agentuser/.hermes/profiles/prefetchbig")
sys.path.insert(0, "/home/agentuser/Projects/hermes-prefetch-compress")

from hermes_state import SessionDB

BIG_DB = Path("/home/agentuser/.hermes/profiles/prefetchbig/state.db")
SID = "20260907_230521_d588b3"
db = SessionDB(db_path=BIG_DB)

blocks = []
for b in range(28):
    lines = []
    for j in range(220):
        lines.append(
            f"audit[{b:02d}:{j:03d}] path=/var/log/service-{b % 9}/app-{j % 7}.log "
            f"ts=2026-09-0{(j % 9) + 1}T10:{j // 60:02d}:{(j % 60):02d}Z level={'INFO' if j % 3 else 'WARN'} "
            f"msg=handler completed request #{100000 + b * 5000 + j} for shopper-id-7f3a9 "
            f"elapsed_ms={50 + (j * 37) % 4000} status={200 if j % 4 else 500} "
            f"src=10.0.{b % 16}.{j % 254} dst=10.1.{(b + j) % 16}.17 body_len={1200 + (j * 911) % 64000}"
        )
    blocks.append(
        f"--- audit dump {b + 1}/28 (batch of service logs, captured verbatim) ---\n"
        + "\n".join(lines)
        + f"\n--- end audit dump {b + 1}/28 ---\n"
        "summary for this batch: request latency p50=1840ms p95=6120ms, errors=4 (2 timeout, 2 5xx), "
        "no retries triggered, cache hit ratio 0.87, quota headroom 61%, no anomaly detected."
    )

msgs = []
tool_idx = 0
for b, content in enumerate(blocks):
    tool_idx += 1
    call_id = f"call_audit_{tool_idx}"
    msgs.append({
        "role": "assistant",
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"}}],
        "content": None,
    })
    msgs.append({"role": "tool", "tool_call_id": call_id, "name": "terminal", "content": content})
    msgs.append({"role": "assistant", "content": (
        f"Captured audit batch {b + 1}/28 verbatim without truncation; waiting on the remaining "
        "batches before the consolidated analysis. No decisions are made until every batch is in — "
        "incomplete data would bias the anomaly detector. shopper-id-7f3a9 correlation stays intact."
    )})

db.append_messages_batch(SID, msgs, chunk_rows=500)
total = sum(len(m.get("content") or "") for m in msgs)
print(f"appended {len(msgs)} rows, {total} chars (~{total // 4} est tokens, ~{int(total * 0.323 + 99000)} predicted real in)")