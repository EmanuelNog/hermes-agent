#!/usr/bin/env python3
"""Benchmark compression post-#96603 - v9. One-shot Continue. per fork (anchor injected into fork).
Sequential with cooldown."""
import subprocess, time, os, re, sys

MODES = [
    ("default", "",         "20260907_230521_d588b3"),
    ("low",     "low",      "20260907_230521_5a165c"),
    ("none",    "none",     "20260907_230521_aa5684"),
]
HERMES = "/home/agentuser/.hermes/hermes-agent/venv/bin/hermes"
LOG = "/home/agentuser/.hermes/logs/agent.log"
COOLDOWN = 60
results = {}

for mode, effort, fork in MODES:
    print(f"\n=== MODE: {mode} (effort='{effort}') fork={fork} start={time.strftime('%H:%M:%S')} ===", flush=True)
    r = subprocess.run(["hermes", "config", "set", "auxiliary.compression.reasoning_effort", effort],
                       capture_output=True, text=True)
    print(f"config set rc: {r.returncode}", flush=True)

    before = open(LOG).read()
    start = time.time()
    r = subprocess.run([HERMES, "chat", "-q", "Continue.", "-r", fork],
                       capture_output=True, text=True, timeout=3000)
    wall = time.time() - start
    after = open(LOG).read()
    new = after[len(before):]
    done = "context compression done: session=" + fork in new
    preflight = "Preflight compression" in new
    deferred = "deferring to the next response" in new or "not anchored" in new
    attempts = new.count("Auxiliary compression: using")
    m = re.search(r'"total_duration_ms":(\d+)', new)
    tele = int(m.group(1)) if m else None
    print(f"{mode}: rc={r.returncode} wall={wall:.1f}s telemetry_ms={tele} done={done} preflight={preflight} deferred={deferred} attempts={attempts}", flush=True)
    results[mode] = {"rc": r.returncode, "wall": wall, "telemetry_ms": tele, "done": done, "preflight": preflight, "deferred": deferred, "attempts": attempts}

    if mode != MODES[-1][0]:
        print(f"--- cooldown {COOLDOWN}s ---", flush=True)
        time.sleep(COOLDOWN)

print("\n===== SUMMARY =====", flush=True)
for mode, res in results.items():
    print(f"{mode}: wall={res['wall']:.1f}s rc={res['rc']} done={res['done']} preflight={res['preflight']} deferred={res['deferred']} attempts={res['attempts']} telemetry_ms={res['telemetry_ms']}", flush=True)
