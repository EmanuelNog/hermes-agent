# Fork notes

Append-only decision log for this fork. One entry per decision: what was decided, why, and
what the rejected alternative would have cost. New entries go at the top of the index and are
added below it, never rewritten in place — superseding an entry means adding a new one that
names it.

| date | entry | state |
|---|---|---|
| 2026-11-14 | [Compaction: no async/prefetch; keep default compression](#2026-11-14--compaction-no-asyncprefetch-keep-default-compression) | active |

---

## 2026-11-14 — Compaction: no async/prefetch, keep default compression

**Decision.** This fork does not carry a compression implementation of its own. No
`compression.prefetch_margin`, no background summary worker, no early adoption. Compaction is
whatever upstream ships, configured through ordinary `compression.*` keys. The prefetch
implementation is preserved on `archive/prefetch-compression`
(tag `archive/prefetch-compression-2026-11-14`, commit `3fec1e3994`) and is not maintained.

### 1. Why the question came up

arXiv:2610.29869 ("Compaction Laundering", Dabirsiaghi, Oct 2026) showed that a harness which
flattens typed messages into labelled text (`[USER]: …`, `[TOOL RESULT <id>]: …`) before
summarizing lets content forge a user turn, and the forged approval survives compaction as a
standing instruction. Measured at 87–100% against five flattening harnesses, 0% against four
role-typed ones.

Hermes was in the first group. Fixed here in `ab0139253f` (chk-n5va.115): role-label tokens
inside content are escaped (`\[USER]`), for the bracketed shape and for the bracket-less
`USER:` shape used by the `/btw` transcript, applied **after** every trim so a cut cannot drop
the escape. That fix is kept — it is not compaction *behaviour*, it is provenance hygiene.

Upstream's own sequence, for the record: `#144835` reported it and was closed 2026-10-29 as
triaged with `#144880` (line-start escape, still open) as interim; the paper's author
recommended role-typed input; `#146750` tracks that and ran the A/B; `#154891` (teknium1) built
it; `#155509` salvages it. Our equivalent PR `#155721` was opened, then closed in favour of
`#154891`. **No release contains any of this** — checked `upstream/main`, `v0.23.1` and the
newest canary, all without `agent/context_compressor_typed_input.py`.

The role-typed change was ported into this fork as `3fec1e3994` (delta-apply; a plain merge of
that branch conflicts on ~727 files because the merge base of a partial clone is a shallow
artifact) and is kept: it removes the forgeable boundary at the source, for free.

### 2. What "prefetch" was, and what upstream now has

The archived implementation armed a background summary below the blocking threshold
(`arm = threshold_tokens − prefetch_margin × context_length`) and **adopted it at the next turn
start**, splicing the persisted middle so the live context shrank early.

Upstream ships a different, narrower feature: `compression.prepare_ahead` (default `false`),
`agent/prepared_compaction.py`. It plans the same window `compress()` would, summarises a deep
copy of that window on a copied compressor, and then **waits for the threshold**: at the next
automatic compaction it adopts the prepared summary if the covered messages are still the same
messages (same session, same head boundary, per-message `message_uid`; text rewrites are
tolerated, a dropped message or rewind is not). One pass per window, cancellation at session
boundaries, failure isolated from the live cooldown.

So upstream already provides the "don't block at the trigger" half. It does **not** adopt early,
which was this fork's only real delta.

### 3. Measured geometry (1M window, lean tail)

- Lean tail budget: `min(25_000, 2.5% of window)`, floor 10K, held to half the trigger →
  **25,000** tokens on a 1M window; 20,000 on an 800K window; 10,000 at or below 400K.
- Prepare band: `threshold_tokens × 0.12` — 78K at `threshold: 0.65`, 63K at 0.5, 50K at 0.4.
  It scales with the trigger; there is no config key for it.
- On adoption the retained verbatim tail is the pass's boundary onward, i.e. the planned tail
  **plus everything that arrived during the pass**, bounded by the band (~up to 88K at 1M/0.5);
  tool results older than the newest 6 rounds are demoted to stubs, which pulls the token cost
  back down.
- The summarizer input is capped at `_SUMMARY_INPUT_MAX_CHARS = 160_000` chars (~40K tokens),
  lean mode even-sampling 8 slices into it, so a pass costs the same regardless of how big the
  middle is.

### 4. Cost, on a real lean-era session

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

- Async premium at a fixed threshold: **+$0.40 (+4%)**, essentially all of it cached reads and
  the cold prefill of a fatter rebuilt context (+312K cold-prefill tokens, +44M cached reads).
- Threshold effect is small in money — the spread across all six rows is ±7%, because fresh
  input (52% of spend) and output (21%) do not shrink with the context; only cache reads (27%)
  do. An earlier model that scaled everything by context size overstated this ~4×.
- The payoff is wall-clock: 4–6.5 compactions × 116–253s = **8–27 minutes** of blocking removed
  per session.
- Band sensitivity: halving the overshoot or the band halves the premium (+$0.22); waste from 0
  to 50% moves it by only +$0.08.

### 5. Why we are not keeping it

1. Upstream's `prepare_ahead` already removes the blocking wait, which was the user-visible
   problem, and it does it with stricter validation (identity vouching) than the fork's
   overlap-stripping.
2. The fork's unique half — early adoption — costs an extra cache-prefix break at the next turn
   start, i.e. an extra cold prefill at a moment the baseline would not have taken one, and its
   benefit after that cost has never been measured on this box (`prefetch_margin` was 0 in every
   profile, so the feature was never armed in production).
3. Every upstream port re-litigated the same files (`context_compressor.py`,
   `conversation_compression.py`, the turn gates) — the conflict cluster of every single port —
   for a feature with a ±7% money effect and no measured token saving.
4. A fork that carries a compaction implementation also cannot drop its upstream-sync discipline
   for a day without drifting from the invariants the other half (provenance, fencing,
   rotation) depends on.

**Rejected alternative:** keep prefetch and enable it (`prefetch_margin: 0.05`,
`threshold: 0.4`). Cost estimate: a few tenths of a dollar per long session, an extra prefix
break per adoption, and permanent port churn. Not worth it for an unmeasured saving.

**Rejected alternative:** ask upstream for a `compression.prepare_band_ratio` key so the band
can be narrowed to arm later with less overshoot. Correct long-term home for that knob, ~4
lines, but it does not change the decision here — default already arms at 12% of the trigger.

### 6. What this fork keeps

- The compaction provenance fix (`ab0139253f`): role-label escape, all three flattening
  surfaces reachable from the compressor and `/btw`.
- The upstream role-typed summarizer input port (`3fec1e3994`), including its tests.
- Everything unrelated to compaction that this fork has always carried.
- Upstream sync continues on this branch, by delta-apply, as before.

### 7. How to reopen

Restore `archive/prefetch-compression` (tag `archive/prefetch-compression-2026-11-14`) onto a
branch, re-apply its commits, and re-enable `compression.prefetch_margin` in the profile. The
earlier retirement also removed the daily-checklist step that drove the sync; if the feature
comes back, that step comes back with it.
