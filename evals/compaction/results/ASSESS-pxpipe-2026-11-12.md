# pxpipe in the compaction study — mechanism measured, recall pending (2026-11-12)

## What pxpipe is

[teamchong/pxpipe](https://github.com/teamchong/pxpipe) (MIT, ~7.7k stars) is a local proxy for
coding agents that cuts *input* tokens by re-pricing context instead of shrinking it: the bulky,
cacheable part of a request (system prompt, tool docs, older history) is rendered into dense PNG
pages in 5×8 Spleen bitmap glyphs and sent as image blocks. An image costs tokens by pixel area, not
by how much text it holds, so dense text buys several times more characters per token. The model
then reads its own history through the vision channel. Recent turns stay text; the current user
message is never imaged.

Its published claims, and their limits (from its own `README.md`, `FINDINGS.md`, `docs/NOT-OCR.md`):

- **Density**: measured on its own corpus, ~18.9 chars per vision-token (~4.7× a 4-chars/token text
  baseline) at production geometry; page = 1568×728 px ≈ 1,456 Anthropic image tokens for ~28k chars.
- **Reader-model gated**: `gemini-4-argon` reads dense pages at 15/15 exact hex and 100/100
  arithmetic; `gemini-3.6/.7/.8-flash` at 14/15; `claude-fable-5` at 13/15. Every later Claude
  release falls to 0–3/15, and `deepseek-v4.1-flash-1017` — **our own default reader** — scores
  **0/15 dense hex at 5×8** (8/15 only at a 14px profile that costs 3× the tokens).
- **Lossy in a specific way**: prose survives density because the language prior repairs it;
  identifiers corrupt, and misses are *plausible* (a wrong hash that looks like a hash), not garbled.
  Hence their bounded text "factsheet" of ~96 identifier tokens carried beside the pages.
- **Workload dependent**: wins on token-dense content (~1 char/token), loses money on sparse prose
  (~3.5 chars/token) — they ship a profitability gate. Savings also track *uncached* bulk.

## How it is wired into this study

`policies.py` gains `"pxpipe"`, `pxpipe_arm.py` implements it, `pxpipe_render.mjs` drives the
renderer. Three deliberate choices:

1. **The renderer is theirs, not a reimplementation.** `pxpipe_render.mjs` imports
   `renderDensePages` from a checkout of the upstream repo (`PXPPIPE_DIR`) through `tsx`. No
   third-party code is vendored into this repo, and pages are byte-identical to proxy output.
2. **Same structure as the shipping policy.** Head and protected tail stay verbatim; only the middle
   region that `current` summarizes is replaced — by pixels of that same region instead of a summary.
   One variable changes: summary text vs pixels of the same span.
3. **Pages are charged into `after_tokens`** (`after_text_tokens` + `after_image_tokens`), so an image
   arm cannot look free. `coverage_pct` records how much of the region was actually sent when
   `max_pages` binds.

Reader-model confound is handled with `EVAL_ANSWER_PROVIDER` / `EVAL_ANSWER_MODEL`, which force the
same route for *every* arm (answers and judge), so a paired `current` vs `pxpipe` run compares the
representation, not the reader.

## Results — the part that is measured (deterministic, no LLM calls)

Two real lineages from `state.db` (reconstructed with `scripts/reconstruct_lineage.py`), capped at
200K and 500K tokens. Region = what compaction collapses; text cost at the harness's 4 chars/token.

| transcript | cap | region chars | text tokens | pages | image tokens | chars/img-token | saving vs text | retained total (text+img) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| gist (2 sessions) | 200K | 590,167 | 147,541 | 22 | 31,136 | 19.0 | 78.9% | 78,826 (47,690 text) |
| gist | 500K | 1,681,961 | 420,490 | 61 | 88,480 | 19.0 | 79.0% | 141,380 (52,900 text) |
| pc2 (5 sessions) | 200K | 590,956 | 147,739 | 22 | 31,360 | 18.8 | 78.8% | 74,811 (43,451 text) |
| pc2 | 500K | 1,719,272 | 429,818 | 63 | 91,168 | 18.9 | 78.8% | 135,183 (44,015 text) |

Raw numbers: `results/pxpipe-2026-11-12/measurements.json`.

What this does and does not say:

- **The density claim holds on our traffic.** 18.8–19.0 chars per image token, ~79% cheaper than
  the same region as text. This is representation cost only.
- **Against our own compaction it is NOT the cheaper arm at equal structure.** At 200K the summary
  path retains ~51K tokens where the image arm retains ~75–79K, because the head and tail stay text
  either way and the pages ride on top: pxpipe re-prices *its* region, while our summary also
  shrinks the total. The image arm's only chance is recall per token, which is exactly what is not
  yet measured.
- **Fidelity side note**: dropped glyphs are negligible and benign — a variation selector, and C1
  control bytes (latin-1 mojibake) from one session's tool output. No identifier characters are
  dropped at this density; the loss, if any, is in the reader.

## Results — the part that is blocked (recall)

Recall cannot be measured today. The arm needs a vision-capable reader, and:

- `nous` (our Zero-tier provider) returned `402 usage_window_and_credits_exhausted` — the daily
  Hermes usage window is used up and resets 2026-11-13 03:28 UTC. Every image request we sent was
  answered only by retry/fallback noise;
- `openrouter` serves `google/gemini-3.8-flash` and read the pages correctly in a 2-page probe, but
  it is a **paid** lane: real spend needs the owner's approval, so it is not used for a 15-question
  run on its own;
- the model our compression actually runs on (`deepseek-v4.1-flash`) is on pxpipe's do-not-image
  list (0/15 dense hex at 5×8).

The one measured reference point, `current` on the gist lineage at 200K cap: **93.3% recall @ 51,068
retained tokens** — but its answer route (pinned to `nous/google/gemini-4-argon` for reader parity)
also degraded into fallbacks during that window, so treat 93.3% as provisional and re-run it
cleanly next to the image arm.

### Free readers do not read the pages — they invent them

Before spending anything, the $0 OpenRouter vision SKUs were probed for verbatim read of four known
lines from a rendered page (exact-match grading, no judge):

| reader | exact lines |
|---|---:|
| `nvidia/nemotron-3.5-super-vl:free` | 0/4 (empty answer) |
| `google/gemma-4-31b-it:free` | 0/4 |
| `thinkingmachines/inkling-2-small:free` | 0/4 |

Worse than zero: the misses are fluent and plausible. Gemma produced `msg100 assistant: deploying
archival shards, calibrating` for a page line that reads `msg100 assistant: edited
evals/compaction/runner.py line 100; commit 3aafa9bb1a1e; error E100: cannot resolve symbol 'x100'`;
inkling repeated "the quick brown fox" over four different rows. This is exactly the failure pxpipe's
own `docs/NOT-OCR.md` predicts — a VLM has no per-glyph stage to fail loudly on, so an unreadable
page yields confident boilerplate rather than an error. It also means an image arm scored by a free
reader would measure the reader's imagination, not pxpipe: the recall comparison needs a Tier-1
reader (nous `gemini-4-argon` after the daily window resets, or paid OpenRouter `gemini-3.8-flash`).

Open question to answer when a healthy vision route exists:

1. `current` and `pxpipe` on the same two lineages, same 15-question bank, same reader, same judge —
   is any recall loss from imaging real at our density, and how many identifier questions does the
   factsheet need to recover?
2. Repeated-compaction behaviour: our 2026-09-19 finding was that a stable prefix stays cache-warm
   and beats arms that re-bill context every turn. A page image is a *stable* prefix for the region
   it replaces, so the cache argument that sank the Jev arm may not apply here — measure it.

## Verdict so far

**Adopt the arm, not the posture.** The measurement machinery is in the repo, the renderer is
upstream's own, and the density half of pxpipe's claim reproduces on our traffic. Whether it belongs
in production hinges on a recall comparison that a vision-reader gate (model availability, and the
fact that our default reader cannot read the pages) blocks today. Do not wire pxpipe into Hermes
compaction before that comparison exists — with `deepseek-v4.1-flash` answering, imaged context
would be read at 0/15 on dense identifiers.

What is worth borrowing regardless, at zero risk: pxpipe's **profitability gate** (image only where
the density math wins) and its **bounded identifier factsheet** beside any lossy representation — the
same lesson our Jev scorecard reached from the other direction (the facts we lose live in
identifiers).
