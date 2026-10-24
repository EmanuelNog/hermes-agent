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

**Run it in an ISOLATED home, never by attaching to the shared gateway.**

`./dev-run.sh -p prefetchdev chat ...` is a thin client: it attaches to whichever
gateway serves the profile. On agentpod that is the default multiplexer at
`~/.hermes`, which runs the MAIN INSTALL's code — so the turn executes the
install's modules, not this checkout's, and a dev client racing that owner can
drag it into a replace/restart cycle that kills live sessions
(bead chk-j3si.119; verified 2026-10-23: the multiplexer answered for
prefetchdev with the install HEAD while the checkout was another commit ahead).
`scripts/prefetch_e2e/e2e_dev_home.sh` gives the E2E a home of its own instead:

```bash
scripts/prefetch_e2e/e2e_dev_home.sh --prepare       # provision home + start ITS gateway (first run installs deps, minutes)
HERMES_HOME=$(scripts/prefetch_e2e/e2e_dev_home.sh --print-home) \
  ~/.hermes/hermes-agent/.venv/bin/python scripts/prefetch_e2e/seed_prefetchdev.py
scripts/prefetch_e2e/e2e_dev_home.sh -- chat -r <seeded-sid>    # then drive 2-3 turns (persistent REPL or -q, see below)
# expect: "Prefetch compression armed at ~... (threshold ..., margin ..., context ...)"
#     then: "Prefetch compaction adopted: N -> M messages, K live suffix rows kept"
scripts/prefetch_e2e/e2e_dev_home.sh --status        # asserts its gateway's code_sha == this checkout's HEAD
scripts/prefetch_e2e/e2e_dev_home.sh --stop
```

What the launcher guarantees:

- `HERMES_HOME` is a scratch dir **outside** `~/.hermes` (`~/.cache/hermes-e2e/prefetchdev-home`
  by default), so no multiplexer claims it and no systemd unit exists for it: the real
  gateway is never a candidate owner, and a client here cannot restart it.
- The checkout's own code boots (sys.path[0] = checkout root), so the CLI **and** the
  unmanaged gateway it spawns stamp this checkout's `code_sha`; `--status` proves it.
- Credentials are the ROOT install's `auth.json`, not the profile's — a profile copy can
  lack the nous portal token, which makes the configured fallback unusable.
- Messaging credentials and the BWS token are stripped from the copied dotenv: a second
  gateway that binds the same Telegram bot refuses to start.
- The profile's `config.yaml` (window, `threshold_tokens`, `prefetch_margin`) is copied
  once; `--rebuild` re-copies. The home's `state.db` is its own, so seeds stay private.

Gotchas:

- **The turn runs in the gateway process, so the background worker survives the client.**
  A one-shot `chat -q -r <sid>` can therefore arm on turn 1 and a second `chat -q -r <sid>`
  adopt it — the old "one-shot exits before adoption" rule applied to an in-process CLI,
  not to this isolated gateway. A REPL (tmux) works too and shows both markers in one run.
- **A stale `context_length_cache.yaml` outranks `model.context_length`.** With the cache
  carrying a 1M window for the model actually used, the arm point goes negative and the
  summarizer is asked for an output it truncates → `Prefetch result discarded (worker_noop)`.
  The launcher deletes the cache with every config copy; if you change the model by hand,
  delete it yourself and restart the isolated gateway.
- Provider headroom is a real dependency: when the primary is rate-limited (an OpenCode
  Go weekly cap shows up as `429 GoUsageLimitError`), the aux compression call falls back
  to `auxiliary.compression.fallback_chain` — point it at a provider whose credentials the
  copied `auth.json` actually carries, or the run dies before any adoption.

Config (profile `prefetchdev`, 2026-10-23 — small window, cheap run):
`model.context_length 64000`, `compression.threshold_tokens 22000`,
`compression.prefetch_margin 0.15` (arm point 12,400; the seed lands ~7.5K, so the
resume turn's own tool growth crosses it mid-turn). The arm line prints all four
numbers, so assert on the line rather than on config files.
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
venv/bin/python scripts/prefetch_e2e/bench_compression.py                  # blocking-path timings parsed from agent.log*
venv/bin/python scripts/prefetch_e2e/bench_compression.py --session <id>   # one session only
venv/bin/python scripts/prefetch_e2e/bench_compression.py --json           # machine-readable
```

`bench_compression.py` is **read-only**: it parses the compression lines Hermes
already writes (`context compression started` / `done` / `attempt telemetry`)
out of `$HERMES_HOME/logs/agent.log*` (override with `--log PATH`) and prints
one row per attempt plus min/median/max of `total_duration_ms`. It does not
drive sessions or touch config — to re-baseline, run a compressing session
first, then read the log.
Baseline (2026-09, post-#96603): 253s / 116s / 159s per pass at ~519K prefill.

## Notes

- The worker lives in the process that runs the turn. In the isolated home that process is
  the E2E's own gateway, which outlives the client — so a one-shot `chat -q` can arm and a
  later run can adopt. Driving a REPL (tmux) still shows both markers in one run.
- Always re-baseline `conversation_history` after adoption — the gates do this;
  if you write a new gate, mirror it.
- Skill `hermes-context-compression` carries the full methodology + pitfalls
  (estimator vs real tokens, alternation-safe seeding, PTY driving).
