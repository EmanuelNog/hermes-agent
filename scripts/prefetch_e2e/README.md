# Prefetch compression — manual E2E harness

Re-validation tooling for the prefetch compaction fork. Unit tests
(`tests/agent/test_prefetch_compaction.py`, `tests/agent/test_prefetch_upstream_compat.py`)
cover the state machine and host seams; these scripts reproduce the FULL loop
behaviour (arm -> keep working -> adopt across turns).

> **PROFILE POLICY (owner decision 2026-09-23 — do not revert):**
> The **automated** E2E that runs with every fork/upstream sync uses
> **`prefetchdev` ONLY** (small-scale, 64K window — fast and cheap; big-context
> compression runs cost too much for per-fork validation).
> **`prefetchbig` is MANUAL-ONLY.** It stays alive for deliberate big-scale
> testing when the owner explicitly asks, but must **never** be wired back into
> the automated test list, the sync pipeline, or CI. Future agents: when
> validating a fork/port, run only the `prefetchdev` steps; treat the
> big-scale section below as documentation for a manual session.

Run after every upstream fetch, once the compat suite is green.
**Upstream update procedure: see `UPSTREAM_SYNC.md` (shallow-repo delta port + checklist).**

## Small-scale (minutes): profile `prefetchdev`

```bash
venv/bin/python scripts/prefetch_e2e/seed_prefetchdev.py        # seed a session with filler + tool pairs
# then drive a PERSISTENT process (the worker dies with a one-shot chat -q):
#   interactive REPL:  ./dev-run.sh -p prefetchdev
#   or a script(1) PTY harness (see skill hermes-context-compression)
# expect: "Prefetch compression armed at ~..." then "... adopted: N -> M messages"
```

Config: `compression.threshold_tokens 40000`, `compression.prefetch_margin 0.15`.
The band must be wider than the largest single tool-result jump or arming gets
skipped (estimator jitter ~3K tokens).

## Big-scale: profile `prefetchbig` — ⚠️ MANUAL-ONLY, NOT part of automated validation

> Do **NOT** run these as part of fork validation or the sync pipeline (cost
> decision 2026-09-23). Run only on explicit owner request, as a deliberate
> manual session. See the PROFILE POLICY note at the top.

```bash
venv/bin/python scripts/prefetch_e2e/import_fork_big.py      # import a big transcript from the main state.db (read-only)
venv/bin/python scripts/prefetch_e2e/inflate_big.py          # add audit filler to reach the band
venv/bin/python scripts/prefetch_e2e/inflate_big2.py         # top-up batch
# drive with a persistent PTY; expect arm ~700K, adoption, next request ~85K
```

Config: `compression.threshold_tokens 0.75`-equivalent on 1M,
`compression.prefetch_margin 0.05` (arm band [700K, 750K)). Reference numbers
(2026-09): armed ~728,809; worker ~6m12s fully occluded; adopted
758->103 messages; next request 85,146 tokens (88% smaller); zero blocking lines.

## Benchmarks

```bash
venv/bin/python scripts/prefetch_e2e/bench_compression.py    # blocking-path timings from agent.log sessions
```

`bench_compression.py` reads `agent.log` compression durations; baseline
(2026-09, post-#96603): 253s / 116s / 159s per pass at ~519K prefill.

## Notes

- The worker only survives in a persistent process; `chat -q` one-shots exit
  before adoption (expected, documented).
- Always re-baseline `conversation_history` after adoption — the gates do this;
  if you write a new gate, mirror it.
- Skill `hermes-context-compression` carries the full methodology + pitfalls
  (estimator vs real tokens, alternation-safe seeding, PTY driving).
