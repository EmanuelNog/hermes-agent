#!/usr/bin/env bash
# Dev launcher for ~/Projects/hermes-prefetch-compress (prefetch-compression branch).
# Runs the DEV CLONE's code with the main install's venv (shared deps),
# against the 'prefetchdev' Hermes profile.
# Usage: dev-run.sh <hermes args...>   e.g.  dev-run.sh -p prefetchdev chat -q "hi"
set -euo pipefail
DEV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$DEV_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$DEV_ROOT/venv/bin/python" -c 'from hermes_cli.main import main; raise SystemExit(main())' "$@"