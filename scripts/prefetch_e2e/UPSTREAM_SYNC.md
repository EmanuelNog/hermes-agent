# Fork ↔ upstream sync

## Published fork

- **Public fork:** https://github.com/EmanuelNog/hermes-agent — feature branch
  `prefetch-compression` (pushed 2026-09-11), anchor tag `upstream-port/2026-09-11`.
- **Local remote:** `origin` = `git@github.com:EmanuelNog/hermes-agent.git`
  (`git push origin prefetch-compression upstream-port/<date>` to refresh).
- **PR link (when wanted):**
  https://github.com/NousResearch/hermes-agent/compare/main...EmanuelNog:hermes-agent:prefetch-compression?expand=1
  — the review doc `website/docs/developer-guide/prefetch-compression.md` is the description.

## Remotes (configured 2026-09-11)

- `origin` = `/home/agentuser/.hermes/hermes-agent` — local main checkout, refreshed
  daily by the checklist (`git fetch` + `reset --hard origin/main`); good fallback
  tip source when GitHub is unreachable.
- `upstream` = `git@github.com:NousResearch/hermes-agent.git` — plain SSH via
  `~/.ssh/id_ed25519`, registered to EmanuelNog (re-registered 2026-09-11; verify
  with `ssh -T git@github.com` → "Hi EmanuelNog!"). Fetch is narrowed to main only
  (`remote.upstream.fetch=+refs/heads/main:...`, `--no-tags`) to keep the shallow
  repo lean.
- Fallback if SSH ever breaks or GitHub rate-limits the git endpoints (HTTP 429 on
  the shared IP — seen before the key was re-registered): temporarily switch to the
  gh-token HTTPS path, fetch, then revert:
  `git config url.https://github.com/.insteadOf git@github.com:` +
  `git config credential.helper '!gh auth git-credential'` → `git fetch upstream` →
  unset both. Porting also works from `origin` (mirror tip, ~hours behind).

## Why not a plain merge

Both the main checkout and this fork are SHALLOW (depth-partial, inherited from the
installer). The commit graph between the fork base and a new upstream tip is NOT
connected locally, so `git merge` cannot find a merge base and refuses. Updates are
ported as a 3-way DELTA — content-identical to a normal merge whose merge-base is the
last common ancestor:

## Port procedure (every upstream update)

```bash
cd ~/Projects/hermes-prefetch-compress
git fetch upstream main                     # or: git fetch origin  (mirror fallback)
NEW=<new-tip-sha>; OLD=$(git rev-parse upstream-port/<previous-date>)
git tag -f upstream-port/<today> $NEW       # anchor for the NEXT delta

git diff --full-index --binary $OLD $NEW > /tmp/delta.patch
git apply --3way --index --whitespace=nowarn /tmp/delta.patch > /tmp/apply.log 2>&1
echo "exit=$?"                              # 0 = applied; inspect conflicts next
git diff --name-only --diff-filter=U        # must be EMPTY; resolve if not, then git add
```

Pitfalls:
- **NEVER pipe `git apply` through `head`/`tail`** — SIGPIPE kills it mid-run and
  NOTHING persists (looks like a silent no-op, rc=0 lies through the pipe).
- Conflicts (if any) cluster in the hot files: `agent/agent_init.py`,
  `agent/conversation_compression.py`, `gateway/run.py`, `hermes_cli/config_defaults.py`,
  `agent/turn_{context_compaction,preflight,overflow,recovery}.py`, `website/docs/...`.
  Resolve = keep upstream's new code + re-insert the prefetch bits.

## Validation checklist (in order)

1. `scripts/run_tests.sh tests/agent/test_prefetch_upstream_compat.py tests/agent/test_prefetch_compaction.py`
   — the compat/drift suite FIRST: failures name the broken seam.
2. Compression family + gateway noise:
   `scripts/run_tests.sh tests/agent/test_*compress*.py tests/agent/test_*compaction*.py tests/gateway/test_telegram_noise_filter.py`
3. Turn loop: `scripts/run_tests.sh tests/agent/test_turn_*.py`
4. Live smoke: seed + tmux REPL per `README.md` in this directory; expect
   `Prefetch compression armed` → `Prefetch compaction adopted` in the profile
   agent.log and the `🗜️`/`✓` report lines in the chat pane.

## Commit the port as a two-parent record

```bash
TREE=$(git write-tree)
MC=$(git commit-tree $TREE -p <previous-branch-tip> -p $NEW -m "merge: port upstream <sha> ...")
git update-ref refs/heads/async-threshold $MC
git log -1 --format="parents: %p"           # both parents recorded
git diff upstream-port/<today> --stat       # should show ONLY our feature
```

## Port log

- **2026-09-11** — upstream `8226c2f6a` ("Merge PR #107708 …"), ported from base
  `ead7e91da`. Delta: 1473 files, +152,439/−9,845; **zero conflicts**. Tests:
  67 (unit+compat) + 922 (compression family + gateway noise) + 133 (turn slice)
  = 1122 passed, 0 failed. E2E: 176 → 96 messages, done line "in 20s".
  Commit `a239a3c4cb`; upstream tag `upstream-port/2026-09-11`.
