# Fork notes

Append-only decision log for this fork. One entry per decision: what was decided, why, and
what the rejected alternative would have cost. New entries go at the top of the index and are
added below it, never rewritten in place — superseding an entry means adding a new one that
names it.

| date | entry | state |
|---|---|---|
| 2026-11-25 | [Compaction: no archive index; upstream's ownership rule stands](#2026-11-25--compaction-no-archive-index-upstreams-ownership-rule-stands) | active |
| 2026-11-14 | [Compaction: no async/prefetch; keep default compression](#2026-11-14--compaction-no-asyncprefetch-keep-default-compression) | active |

---

## 2026-11-25 — Compaction: no archive index; upstream's ownership rule stands

**Decision.** The fork carries no archive-list implementation of its own. The
`cache/pruned/<session>.index.jsonl` file and the one-line summary reference are retired; the committed
summary follows upstream's rule — a stored handoff's list stays on its `state.db` row, and each summary
lists only the archives its own compaction made. Upstream `#159923` (`b80068d4..cf0eb13c`) is ported
verbatim. The retired implementation is preserved on `archive/compaction-archive-index-2026-11-25`
(install build, `f193a5e9b2`) and `archive/compaction-archive-index-pr-2026-11-25` (PR draft,
`79a77230dc`) and is not maintained.

### Why upstream's rule replaces it

The index collapsed the flat `Archived tool output` appendix (measured: 491 KB of a 522 KB summary,
3,585 paths) to one line backed by a JSON row per archive (`path, tool, args_preview, call_id, ts,
chars, sha256`), and `_expand_index_refs` kept every listed archive referenced by the retention sweep.
Upstream removes the cause instead: `named` ignores a stored handoff's own list (lists never re-stack),
`strip_archive_list` keeps the block out of the summarizer prompt, and the "still named" set is computed
from the rows assembly keeps. Residual difference: one compaction's own list is still flat, bounded by
that window's drops rather than cumulative; the index made it one line and added the call join. Two
mechanisms for one reference is drift this fork does not keep, so the smaller upstream rule wins.

**Rejected.** Keeping the index beside upstream's rule (two mechanisms for one reference, plus an
assembly-side strip to stop a copied block), and capping the list (PR `#158892`, closed unmerged: it
drops recovery paths). Cost of either: permanent divergence in the hottest compaction files.

---

## 2026-11-14 — Compaction: no async/prefetch, keep default compression

**Decision.** This fork carries no compaction implementation of its own. No
`compression.prefetch_margin`, no background summary worker, no early adoption. Compaction is
whatever upstream ships, configured through ordinary `compression.*` keys (`prepare_ahead:
false`, `threshold` per profile). The prefetch implementation is preserved on
`archive/prefetch-compression` (tag `archive/prefetch-compression-2026-11-14`, commit
`3fec1e3994`) and is not maintained.

### 1. What prefetch was, and what upstream already has

The archived implementation armed a background summary **below** the blocking threshold
(`arm = threshold_tokens − prefetch_margin × context_length`) and adopted it at the **next turn
start**, splicing the persisted middle so the live context shrank early.

Upstream ships a narrower feature: `compression.prepare_ahead` (default `false`),
`agent/prepared_compaction.py`. It plans the same window `compress()` would, summarises a deep
copy of that window on a copied compressor, and then **waits for the threshold**: at the next
automatic compaction it adopts the prepared summary if the covered messages are still the same
messages (same session, same head boundary, per-message `message_uid`; text rewrites are
tolerated, a dropped message or rewind is not). One pass per window, cancelled at session
boundaries, failures isolated from the live cooldown.

So the "don't block at the trigger" half is already upstream, with stricter validation than the
fork had. The fork's only real delta was the **early adoption** — shrinking the persisted middle
before the threshold is crossed.

### 2. Measured geometry (1M window, lean tail)

- Lean tail budget: `min(25_000, 2.5% of window)`, floor 10K, held to half the trigger →
  **25,000** tokens on a 1M window; 20,000 on an 800K window; 10,000 at or below 400K.
  Unaffected by the threshold on a 1M window.
- Prepare band: `threshold_tokens × 0.12` — 78K at `threshold: 0.65`, 63K at 0.5, 50K at 0.4.
  It scales with the trigger, and there is **no config key** for it (`PREPARE_BAND_RATIO` is a
  module constant), so the arming point cannot be moved without editing core.
- Adoption retains the pass's boundary onward, i.e. the planned tail **plus everything that
  arrived during the pass**, bounded by the band (up to ~88K at 1M/0.5). Tool results older than
  the newest 6 rounds are demoted to stubs, which pulls the token cost back down.
- The summarizer input is capped at `_SUMMARY_INPUT_MAX_CHARS = 160_000` chars (~40K tokens),
  lean mode even-sampling 8 slices into it, so a pass costs the same however big the middle is —
  firing earlier does not make the pass shorter.

### 3. Cost, on a real lean-era session

Base: `20260915_144353_61f9a1` — 245h, 220 messages, 1,124 main-loop calls, 4 compactions,
deepseek-v4.1-flash via opencode-go, 2.08M tokens grown. Measured: 14.1M fresh input, 286.6M
cache-read, 1.21M output; per compaction 49,722 in / 23,820 out. Prices from
`agent/usage_pricing.py`: $0.30/M in, $0.006/M cache-read, $1.20/M out.

| threshold | mode | compactions | summary $ | cache $ | output $ | total |
|---|---|---|---|---|---|---|
| 0.65 | blocking (actual) | 4.0 | 0.174 | 7.59 | 1.99 | **9.75** |
| 0.65 | async | 4.0 | 0.218 | 7.94 | 1.99 | **10.15** |
| 0.50 | blocking | 5.2 | 0.226 | 7.00 | 1.99 | **9.22** |
| 0.40 | blocking | 6.5 | 0.283 | 6.62 | 1.99 | **8.89** |
| 0.40 | async | 6.5 | 0.353 | 6.87 | 1.99 | **9.22** |

- Async premium at a fixed threshold: **+$0.40 (+4%)**, essentially all of it cache: +312K
  cold-prefill tokens and +44M cached reads, from the fatter rebuilt context and the shorter
  refill range.
- The threshold's money effect is small — the spread across all six rows is ±7% — because fresh
  input (52% of spend) and output (21%) do not shrink with the context; only cache reads (27%)
  do. An earlier model that scaled everything by context size overstated this ~4×.
- The payoff is wall-clock: 4–6.5 compactions × 116–253s = **8–27 minutes** of blocking removed
  per session.
- Band sensitivity: halving the overshoot or the band halves the premium (+$0.22); waste from 0
  to 50% moves it by only +$0.08.

### 4. Why we are not keeping it

1. `prepare_ahead` already removes the blocking wait, which was the user-visible problem, with
   stricter validation (identity vouching) than the fork's overlap-stripping.
2. The fork's unique half — early adoption — costs an extra cache-prefix break at the next turn
   start, an extra cold prefill at a moment the baseline would not have taken one, and its
   benefit after that cost has never been measured on this box (`prefetch_margin` was 0 in every
   profile, so the feature never armed in production).
3. Every upstream port re-litigated the same files (`context_compressor.py`,
   `conversation_compression.py`, the turn gates) — the conflict cluster of every port — for a
   feature with a ±7% money effect and no measured token saving.
4. A fork carrying its own compaction cannot skip its sync discipline for a day without drifting
   from the invariants the blocking path depends on (fencing, rotation, commit boundaries).

**Rejected:** keep prefetch and enable it (`prefetch_margin: 0.05`, `threshold: 0.4`). A few
tenths of a dollar per long session, an extra prefix break per adoption, and permanent port
churn, for an unmeasured saving.

**Rejected:** ask upstream for a `compression.prepare_band_ratio` key so the band can be narrowed
(arm later, less overshoot, less waste). Correct long-term home for that knob, ~4 lines, but it
does not change this decision — the default already arms at 12% of the trigger.

### 5. What this changes in the tree

Branch `custom-main` at `1a9ac326f2` removes the feature (agent module, five gate call sites, the
quiet-status plumbing and status templates with their gateway noise regexes, the `agent_init`
parsing, the `config_defaults` key, four test files plus the prefetch cases in two others, the
E2E harness, both docs, the sidebar entry) and keeps everything else this fork has always
carried. Compaction behaviour from here on is upstream's.

### 6. How to reopen

Restore `archive/prefetch-compression` onto a branch, re-apply its commits, re-enable
`compression.prefetch_margin` in the profile, and restore the daily-checklist step's prefetch
gate in `hermes-fork-sync`.
