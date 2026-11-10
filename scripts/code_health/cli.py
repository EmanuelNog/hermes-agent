"""``python -m scripts.code_health`` / ``scripts/check``: the code-health ratchet."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from scripts.code_health import gitio
from scripts.code_health.compare import compare
from scripts.code_health.config import in_scope
from scripts.code_health.measure import Measurer
from scripts.code_health.report import (
    apply_allows,
    format_findings,
    summarize_tree,
    verdict,
)
from scripts.code_health.ruff_runner import pinned_version, resolve_ruff

_FOOTER = """
Each function and file has its own cap: its value on main if it is already over target,
otherwise the target. New code meets the target; existing code may only go down. Fix the code
(the `fix:` line is the repo's remedy); a genuine exception takes a reviewed comment on the line,
`# health: allow <RULE> -- <why>`. Rules and targets: scripts/code_health/config.py.
Reproduce locally: python scripts/check --only health (every lint check: python scripts/check;
on every push: python scripts/check --install-hook pre-push)."""

_SWITCH_FILE = "scripts/code_health/config.py"
_MODES = ("blocking", "advisory", "off")
# An implicit base (the merge-base with origin/main) can be months stale, and then EVERY file
# differs: a local `python scripts/check --only health` measured 29,520 file-pairs (~0.3 s each)
# with no output for hours (chk-n5va.97). Bounded, and loud about why (#chk-n5va.97).
_MAX_DIFF_FILES = 2000



def parse_switch(text: str) -> str:
    """The module-level ``ENFORCEMENT`` value (plain or annotated assignment; the last one wins,
    as at runtime). Missing, unparseable or unknown is "blocking", so no edit relaxes it by
    accident."""
    try:
        body = ast.parse(text).body
    except SyntaxError:
        return "blocking"
    found = None
    for node in body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "ENFORCEMENT" for t in targets):
            found = node.value.value if isinstance(node.value, ast.Constant) else None
    return found if found in _MODES else "blocking"


def enforcement(repo: Path, rev: str) -> str:
    """The master switch as committed on ``rev``; a revision without one is blocking."""
    return parse_switch(gitio.read_file(repo, rev, _SWITCH_FILE) or "")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="code_health", description=__doc__)
    p.add_argument("--base", help="base revision (default: merge-base of HEAD with origin/main)")
    p.add_argument("--head", help="head commit or tree (default: the working tree, untracked included)")
    p.add_argument("--report", action="store_true", help="burn-down summary of the whole head tree")
    p.add_argument("--max-files", type=int, default=_MAX_DIFF_FILES, metavar="N",
                   help="refuse an implicit-base run over N measured files (an explicit --base is "
                        f"never capped; 0 = no limit). Default {_MAX_DIFF_FILES}")
    p.add_argument("--json", action="store_true", help="machine-readable findings")
    p.add_argument("--print-pins", action="store_true",
                   help="print the pinned ruff requirement (CI installs exactly this)")
    return p


def _report(repo: Path, head: str | None) -> int:
    paths = [p for p in gitio.tracked_files(repo, head) if in_scope(p)]
    measurer = Measurer(repo, resolve_ruff(repo), known_env=gitio.known_env_names(repo, head))
    print(summarize_tree(measurer.measure(head, paths)))
    return 0


def _skipped(why: str, as_json: bool) -> None:
    """A run that measured nothing: an empty findings list for --json, the reason for humans."""
    if as_json:
        print("[]")
    print(why, file=sys.stderr if as_json else sys.stdout)


def _implicit_base_refusal(base: str, total: int, max_files: int) -> str:
    """The loud refusal when an implicit base makes the whole tree "changed".

    Only ever raised for a base nobody chose (the merge-base with origin/main), so a caller who
    passed ``--base`` — CI, a hook, a human — is never second-guessed.
    """
    return (
        f"{total} files differ from the implicit base {base[:12]}, more than --max-files {max_files}:"
        " that is the whole tree, so the base is stale (origin/main has not moved while this branch"
        " grew) and a ratchet against it would judge every file in the repo — hours of measuring"
        " with nothing to report. Pass --base REV (the commit this work started from, e.g. the last"
        " sync merge), refresh origin/main, or --max-files 0 to measure the whole tree anyway."
    )


def _upstream_ref(repo: Path, tip: str) -> str | None:
    """The ref this branch pushes to (``@{upstream}``), when it names a commit."""
    out = gitio.git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                    f"{tip}@{{upstream}}", check=False).strip()
    return out if out and "@{" not in out and not out.startswith("fatal") else None


def _implicit_base(repo: Path, tip: str, head: str | None,
                   max_files: int) -> tuple[str, str | None]:
    """The base nobody chose, plus a note when the obvious one had to be given up.

    ``origin/main`` is upstream's truth, but a fork never sees it move and a long-lived branch
    outruns it: the merge-base is then months old and EVERY file differs — 20 846 measured on the
    hermes fork, which is how a local ``scripts/check --only health`` came to measure the whole
    tree for eight hours with a 0-byte log (chk-n5va.97). When that base is runaway, the branch's
    own upstream is tried next, since it is the revision the work is actually diffed against on
    push; only when that is runaway too does the caller get the refusal.
    """
    first = gitio.default_base(repo, tip)
    if max_files <= 0:
        return first, None
    total = len(gitio.changed_files(repo, first, head))
    if total <= max_files:
        return first, None
    upstream = _upstream_ref(repo, tip)
    if upstream:
        alt = gitio.git(repo, "merge-base", tip, upstream, check=False).strip()
        if alt and alt != first and len(gitio.changed_files(repo, alt, head)) <= max_files:
            return alt, (f"{total} files differ from {first[:12]} (stale: the base ref has not moved"
                         f" while this branch grew — an upstream main never advances on a fork),"
                         f" so the implicit base is {upstream} ({alt[:12]}) instead")
    raise RuntimeError(_implicit_base_refusal(first, total, max_files))


def run(repo: Path, base: str, head: str | None, as_json: bool = False,
        switch_rev: str | None = None, max_files: int = 0) -> int:
    """Judge ``head`` against ``base``; the ENFORCEMENT switch is read from ``switch_rev``
    (default: the base), never from the head under test. ``max_files`` refuses a runaway diff
    over an implicit base (``0`` = unbounded) — see :func:`_implicit_base_refusal`."""
    started = time.monotonic()
    switch_rev = switch_rev or base
    mode = enforcement(repo, switch_rev)
    if mode == "off":
        _skipped(f"code health: off (ENFORCEMENT in {_SWITCH_FILE} on {switch_rev[:12]})", as_json)
        return 0
    changes = gitio.changed_files(repo, base, head)
    head_paths = sorted({c.new for c in changes if c.new and in_scope(c.new)})
    base_paths = sorted({c.old for c in changes if c.old and in_scope(c.old)})
    if not head_paths:
        _skipped("code health: no measured files changed", as_json)
        return 0
    if max_files > 0 and len(head_paths) + len(base_paths) > max_files:
        raise RuntimeError(_implicit_base_refusal(base, len(head_paths) + len(base_paths), max_files))
    # Say what is about to be measured before measuring it: the stall this replaced emitted
    # nothing at all for eight hours.
    print(f"code health: measuring {len(head_paths)} changed + {len(base_paths)} base files"
          f" against {base[:12]}", file=sys.stderr)
    # Base files are measured too: a deleted .py still needs ruff on the base side.

    needs_ruff = any(p.endswith(".py") for p in (*head_paths, *base_paths))
    ruff = resolve_ruff(repo) if needs_ruff else []
    measurer = Measurer(repo, ruff, known_env=gitio.known_env_names(repo, base))
    base_m = measurer.measure(base, base_paths)
    head_m = measurer.measure(head, head_paths)
    findings = compare(base_m, head_m, changes)
    apply_allows(findings, head_m)
    blocking, advisory = verdict(findings)
    failed = bool(blocking) and mode == "blocking"
    if as_json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
        return 1 if failed else 0
    body = format_findings(findings)
    if body:
        print(body)
    elapsed = time.monotonic() - started
    print(f"\ncode health: {len(head_paths)} files vs {base[:12]}: {blocking} blocking, "
          f"{advisory} advisory ({elapsed:.1f}s)")
    if blocking:
        print(_FOOTER)
    if blocking and not failed:
        print(f"\nadvisory mode (ENFORCEMENT in {_SWITCH_FILE}): not failing on the above.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = gitio.repo_root(Path.cwd())
    if args.print_pins:
        print(f"ruff=={pinned_version(repo)}")
        return 0
    try:
        head = gitio.resolve_tree(repo, args.head) if args.head else None
        if args.report:
            return _report(repo, head)
        if args.base:  # CI: the base is the target branch tip, which also holds the switch
            return run(repo, gitio.resolve_rev(repo, args.base), head, as_json=args.json)
        # Locally the base is the merge-base, which lags main; the switch comes from the main tip
        # so a flip on main reaches a branch without a rebase. The head never supplies it.
        tip = gitio.commit_or_none(repo, args.head) if args.head else None
        base, note = _implicit_base(repo, tip or "HEAD", head, args.max_files)
        if note:
            print(f"code health: {note}", file=sys.stderr)
        switch_rev = gitio.commit_or_none(repo, "origin/main") or base
        return run(repo, base, head, as_json=args.json, switch_rev=switch_rev,
                   max_files=args.max_files)
    except RuntimeError as exc:
        print(f"code health: {exc}", file=sys.stderr)
        return 2
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # A measuring tool (npm install of the pinned TypeScript, ...) failed: that is a broken
        # run, not a finding, so it must not exit 1 behind a traceback.
        print(f"code health: measurement tooling failed: {exc}", file=sys.stderr)
        return 2
