#!/usr/bin/env python3
"""Benchmark the compression blocking path from a Hermes agent.log.

READ-ONLY: this script only parses lines Hermes already wrote to its log —
`context compression started`, `context compression done` and the
`context compression attempt telemetry` JSON blob. It never mutates config,
never drives a session, and never hardcodes a session id: pass `--session` to
filter, or read everything.

For the full prefetch E2E loop (arm -> keep working -> adopt across turns) see
`seed_prefetchdev.py` plus a persistent process, per README.md.

Usage
-----
venv/bin/python scripts/prefetch_e2e/bench_compression.py                 # every agent.log* under $HERMES_HOME/logs
venv/bin/python scripts/prefetch_e2e/bench_compression.py --log /path/to/agent.log
venv/bin/python scripts/prefetch_e2e/bench_compression.py --session 20261001_164619_42ad5f3a
venv/bin/python scripts/prefetch_e2e/bench_compression.py --json           # machine-readable
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
from pathlib import Path

TS = r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+)"
TEL_RE = re.compile(TS + r" \w+ .*context compression attempt telemetry: (\{.*\})\s*$")
START_RE = re.compile(TS + r" \w+ .*context compression started: session=(\S+) messages=(\d+) tokens=~([\d,]+)")
DONE_RE = re.compile(TS + r" \w+ .*context compression done: session=(\S+) messages=(\d+)->(\d+) rough_tokens=~([\d,]+)")


def default_logs() -> list[str]:
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    found = sorted(glob.glob(str(home / "logs" / "agent.log*")))
    return found or [str(home / "logs" / "agent.log")]


def parse(paths: list[str], session: str | None) -> tuple[list[dict], int, int]:
    attempts: list[dict] = []
    started = done = 0
    for path in paths:
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
            continue
        for line in lines:
            m = TEL_RE.search(line)
            if m:
                try:
                    tel = json.loads(m.group(2))
                except ValueError:
                    continue
                if session and tel.get("session_id") != session:
                    continue
                attempts.append(
                    {
                        "time": m.group(1),
                        "session": tel.get("session_id"),
                        "messages": f"{tel.get('messages_before')}->{tel.get('messages_after')}",
                        "tokens": f"{tel.get('tokens_before')}->{tel.get('tokens_after')}",
                        "reclaimed": tel.get("tokens_reclaimed"),
                        "total_ms": tel.get("total_duration_ms"),
                        "summary_ms": tel.get("summary_generation_ms"),
                        "prompt_ms": tel.get("prompt_build_ms"),
                        "commit_ms": tel.get("commit_ms"),
                        "method": tel.get("method"),
                        "trigger": tel.get("trigger_source"),
                        "commit_status": tel.get("commit_status"),
                        "log": path,
                    }
                )
                continue
            m = START_RE.search(line)
            if m and (not session or m.group(2) == session):
                started += 1
                continue
            m = DONE_RE.search(line)
            if m and (not session or m.group(2) == session):
                done += 1
    attempts.sort(key=lambda a: (a["time"], a["session"] or ""))
    return attempts, started, done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", action="append", metavar="PATH",
                    help="agent.log to read (repeatable; default: $HERMES_HOME/logs/agent.log*)")
    ap.add_argument("--session", help="only report this session id")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args(argv)

    paths = args.log or default_logs()
    attempts, started, done = parse(paths, args.session)
    if args.json:
        print(json.dumps({"logs": paths, "started": started, "done": done, "attempts": attempts}, indent=2))
        return 0 if attempts else 1
    if not attempts:
        print(f"no compression telemetry found in: {', '.join(paths)}", file=sys.stderr)
        print("(run a session that compresses, or point --log at the log that has it)", file=sys.stderr)
        return 1

    print(f"logs: {', '.join(paths)}")
    print(f"passes: {started} started / {done} done; attempts with telemetry: {len(attempts)}\n")
    header = f"{'time':<21} {'session':<24} {'messages':<14} {'tokens':<24} {'total_ms':>9} {'llm_ms':>7} {'method':<12} {'trigger':<8}"
    print(header)
    print("-" * len(header))
    for a in attempts:
        print(
            f"{a['time']:<21} {str(a['session']):<24} {a['messages']:<14} {a['tokens']:<24} "
            f"{str(a['total_ms']):>9} {str(a['summary_ms']):>7} {str(a['method']):<12} {str(a['trigger']):<8}"
        )

    totals = [a["total_ms"] for a in attempts if isinstance(a["total_ms"], int)]
    reclaimed = sum(a["reclaimed"] for a in attempts if isinstance(a["reclaimed"], int))
    print("\nsummary (blocking-path total_duration_ms, from these logs only):")
    print(f"  attempts={len(totals)} min={min(totals)} median={int(statistics.median(totals))} max={max(totals)}")
    print(f"  tokens reclaimed={reclaimed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
