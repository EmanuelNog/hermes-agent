#!/usr/bin/env bash
# Dev launcher for ~/Projects/hermes-prefetch-compress (prefetch-compression branch).
# Runs the DEV CLONE's code with the main install's runtime environment (shared
# deps via the PM-activated environment, hermes_bootstrap), against the
# 'prefetchdev' Hermes profile.
# Usage: dev-run.sh <hermes args...>   e.g.  dev-run.sh -p prefetchdev chat -q "hi"
set -euo pipefail
DEV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The checkout's own venv/ is BROKEN (py3.14 toolchain without deps — the
# runtime got dependency management via `pm` activation). Mirror the real
# launcher (~/.local/bin/hermes): tools python, -I isolation, sys.path ->
# DEV_ROOT, then let hermes_bootstrap run PM dependency activation.
TOOLS_PY="$(ls -d "$HOME/.hermes/tools"/python-*/bin/python3 2>/dev/null | head -1)"
if [ -z "${TOOLS_PY:-}" ] || [ ! -x "$TOOLS_PY" ]; then
  echo "dev-run: no runtime python under \$HOME/.hermes/tools — cannot boot" >&2
  exit 1
fi

exec "$TOOLS_PY" -I -c '
import os, re, sys
os.environ.pop("PYTHONHOME", None)
os.environ.pop("PYTHONPATH", None)
sys.path.insert(0, sys.argv.pop(1))
from hermes_constants import get_default_hermes_root
os.environ["HERMES_HOME"] = os.environ.get("HERMES_HOME") or str(get_default_hermes_root())
import hermes_bootstrap
from hermes_cli.main import main
sys.argv[0] = re.sub(r"(-script\.pyw|\.exe)?$", "", sys.argv[0])
sys.exit(main())
' "$DEV_ROOT" "$@"