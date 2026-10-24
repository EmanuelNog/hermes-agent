#!/usr/bin/env bash
# Run the prefetch E2E against THIS CHECKOUT's code in an ISOLATED home.
#
# Why this exists (bead chk-j3si.119): `./dev-run.sh -p prefetchdev chat ...` is a
# THIN CLIENT. It attaches to whatever gateway serves the profile — on this host the
# default multiplexer at ~/.hermes, which runs the MAIN INSTALL's code, not this
# checkout's (verified 2026-10-23: the multiplexer answered for prefetchdev with
# code_sha 0f8f343, the install HEAD, while the checkout was at b01e2b7). The agent
# turn then executes main's modules, so the settings under test are not the ones
# running, and a dev client racing the shared owner can drag it into a replace or
# restart cycle that costs live sessions their process.
#
# This launcher gives the E2E a home of its own:
#   * HERMES_HOME points at a scratch directory OUTSIDE ~/.hermes, so no multiplexer
#     claims it, no systemd unit exists for it, and the real gateway is never a
#     candidate owner;
#   * this checkout's code boots (sys.path[0] = checkout root), so the CLI and the
#     unmanaged gateway it spawns both stamp this checkout's code_sha;
#   * that gateway exits by itself when idle (--idle-exit); --stop is the manual way.
#
# Credentials: the ROOT install's auth.json is copied, not the profile's. A profile
# auth.json can lack the nous portal token, which makes the configured fallback
# provider unusable and turns any primary 429 into a dead E2E.
#
# Usage:
#   scripts/prefetch_e2e/e2e_dev_home.sh --prepare   # create home, start its gateway, wait for ready
#   scripts/prefetch_e2e/e2e_dev_home.sh --status
#   scripts/prefetch_e2e/e2e_dev_home.sh --stop
#   scripts/prefetch_e2e/e2e_dev_home.sh --print-home
#   scripts/prefetch_e2e/e2e_dev_home.sh -- <hermes args...>     # client in that home
#
# Typical E2E:
#   scripts/prefetch_e2e/e2e_dev_home.sh --prepare
#   HERMES_HOME=$(scripts/prefetch_e2e/e2e_dev_home.sh --print-home) \
#     ~/.hermes/hermes-agent/.venv/bin/python scripts/prefetch_e2e/seed_prefetchdev.py
#   scripts/prefetch_e2e/e2e_dev_home.sh -- chat -r <seeded-sid>   # then drive 2-3 turns
set -euo pipefail

DEV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_PROFILE="${E2E_SRC_PROFILE:-$HOME/.hermes/profiles/prefetchdev}"
SRC_AUTH="${E2E_SRC_AUTH:-$HOME/.hermes/auth.json}"
DEV_HOME="${E2E_HOME:-$HOME/.cache/hermes-e2e/prefetchdev-home}"
# The checkout's PM environment is keyed to the install home, so an isolated home has
# none: boot with the install venv's interpreter (dependency set) and put THIS checkout
# first on sys.path (code under test). Override with E2E_PYTHON.
PY="${E2E_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python}"
REBUILD=0
ENSURE_TIMEOUT="${E2E_ENSURE_TIMEOUT:-900}"

die() { echo "e2e_dev_home: $*" >&2; exit 1; }

ACTION=run
while [ $# -gt 0 ]; do
  case "$1" in
    --home) DEV_HOME="$2"; shift 2 ;;
    --rebuild) REBUILD=1; shift ;;
    --prepare|--status|--stop|--print-home) ACTION="${1#--}"; shift ;;
    --help|-h) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    --) shift; break ;;
    *) break ;;
  esac
done

# --- guard: never point this at a real Hermes root ---------------------------------
mkdir -p "$(dirname "$DEV_HOME")"
case "$(cd "$(dirname "$DEV_HOME")" && pwd)/$(basename "$DEV_HOME")" in
  "$HOME/.hermes"|"$HOME/.hermes/profiles/"*)
    die "refusing to use $DEV_HOME: an isolated E2E home must live OUTSIDE ~/.hermes and its profiles/ (else the default multiplexer claims it and the E2E runs main's code again)" ;;
esac
[ -d "$SRC_PROFILE" ] || die "source profile not found: $SRC_PROFILE"
[ -x "$PY" ] || die "no interpreter at $PY (set E2E_PYTHON)"

json_field() {  # json_field <file> <key>
  "$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$1" "$2" 2>/dev/null || true
}

provision() {
  mkdir -p "$DEV_HOME"/{logs,state,sessions}
  chmod 700 "$DEV_HOME"
  local f
  for f in config.yaml SOUL.md .env; do
    [ -f "$SRC_PROFILE/$f" ] || continue
    if [ ! -f "$DEV_HOME/$f" ] || [ "$REBUILD" = 1 ] || [ "$SRC_PROFILE/$f" -nt "$DEV_HOME/$f" ]; then
      cp -f "$SRC_PROFILE/$f" "$DEV_HOME/$f"; chmod 600 "$DEV_HOME/$f"
      echo "e2e_dev_home: copied $f from $SRC_PROFILE"
    fi
  done
  # A model-metadata context cache outranks `model.context_length`: a stale entry
  # (e.g. 1,048,576 for a nous model) makes the arm point negative and asks the
  # summarizer for a huge output that comes back truncated (worker_noop). Drop it
  # whenever the config is (re)copied so the window under test is the configured one.
  rm -f "$DEV_HOME/context_length_cache.yaml" "$DEV_HOME/context_length_cache.yaml.lock"
  local auth_src="$SRC_AUTH"
  [ -f "$auth_src" ] || auth_src="$SRC_PROFILE/auth.json"
  if [ -f "$auth_src" ] && { [ ! -f "$DEV_HOME/auth.json" ] || [ "$REBUILD" = 1 ] || [ "$auth_src" -nt "$DEV_HOME/auth.json" ]; }; then
    cp -f "$auth_src" "$DEV_HOME/auth.json"; chmod 600 "$DEV_HOME/auth.json"
    echo "e2e_dev_home: copied credentials from $auth_src"
  fi
  # Auth pools may reference env vars (source: env:NAME); carry the install's dotenv,
  # minus messaging credentials and the vault token. Both would hand this gateway a
  # messaging bot token it cannot bind (`telegram: token already in use by the 'default'
  # profile gateway`) — BWS injects TELEGRAM_BOT_TOKEN from the vault even when the dotenv
  # line is dropped — and the E2E never needs a platform or vault sync.
  if [ -f "$HOME/.hermes/.env" ] && { [ ! -f "$DEV_HOME/install.env" ] || [ "$REBUILD" = 1 ]; }; then
    grep -vE '^(TELEGRAM|DISCORD|SLACK|SIGNAL|WHATSAPP|MATRIX|IMESSAGE|SMS|TWILIO|BWS_ACCESS_TOKEN)' \
      "$HOME/.hermes/.env" > "$DEV_HOME/install.env" || true
    chmod 600 "$DEV_HOME/install.env"
    echo "e2e_dev_home: copied install dotenv (install.env, messaging keys dropped)"
  fi
}

live_pid() {
  local f="$DEV_HOME/gateway_state.json" pid
  [ -f "$f" ] || return 1
  pid="$(json_field "$f" pid)"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  [ "$pid" -gt 1 ] && kill -0 "$pid" 2>/dev/null && printf '%s\n' "$pid"
}

status_home() {
  local pid sha head
  head="$(git -C "$DEV_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "home:     $DEV_HOME"
  echo "checkout: $head"
  if pid="$(live_pid)"; then
    sha="$(json_field "$DEV_HOME/gateway_state.json" code_sha)"
    echo "gateway:  pid $pid, code_sha ${sha:-?}, state $(json_field "$DEV_HOME/gateway_state.json" gateway_state)"
    if [ "$sha" = "$head" ]; then echo "verdict:  RUNNING THIS CHECKOUT"; else echo "verdict:  NOT this checkout's code — stop it (--stop) and re-run"; fi
  else
    echo "gateway:  none (--prepare starts one from this checkout)"
  fi
}

# Boot this checkout's CLI in the isolated home: same shape as dev-run.sh, with the
# install venv's interpreter because an isolated home has no PM environment.
boot() {
  export HERMES_HOME="$DEV_HOME"
  # Inherited env would defeat the isolation: a BWS_ACCESS_TOKEN in this shell (the agent's
  # own shell carries one) makes the child sync the vault and inject a messaging bot token
  # it cannot bind, and a stray TELEGRAM_* does the same. Drop both before the dotenv load.
  unset BWS_ACCESS_TOKEN TELEGRAM_BOT_TOKEN HERMES_GATEWAY_SESSION HERMES_CRON_SESSION
  if [ -f "$DEV_HOME/install.env" ]; then set -a; . "$DEV_HOME/install.env" >/dev/null 2>&1 || true; set +a; fi
  cd "$DEV_ROOT"
  exec "$PY" -I -c '
import os, sys
os.environ.pop("PYTHONHOME", None); os.environ.pop("PYTHONPATH", None)
sys.path.insert(0, sys.argv.pop(1))
import hermes_bootstrap
from hermes_cli.main import main
sys.argv[0] = "hermes"
sys.exit(main())
' "$DEV_ROOT" "$@"
}

case "$ACTION" in
  print-home) printf '%s\n' "$DEV_HOME"; exit 0 ;;
  status) provision >/dev/null; status_home; exit 0 ;;
  stop)
    if pid="$(live_pid)"; then
      kill -TERM "$pid" 2>/dev/null || true
      for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
      kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
      echo "e2e_dev_home: stopped isolated gateway pid $pid"
    else
      echo "e2e_dev_home: no isolated gateway running"
    fi
    exit 0 ;;
  prepare)
    provision
    status_home >&2
    # `gateway ensure` installs this home's runtime on first use (minutes) and starts
    # the daemon; a chat -q would give up at its own, shorter deadline.
    boot gateway ensure --timeout "$ENSURE_TIMEOUT" >&2
    status_home ;;
  run)
    provision
    status_home >&2
    boot "$@" ;;
esac
