#!/usr/bin/env bash
#
# start.sh — launch RetailGex and its background services from the terminal.
#
#   ./start.sh              Web app + scheduler + MCP server (Ctrl-C stops all)
#   ./start.sh --tunnel     Also start the cloudflared tunnel
#   ./start.sh --web-only   Web app only (no scheduler, no MCP server)
#
# Loads .env into the environment so gunicorn.conf.py picks up
# FLASK_HOST/FLASK_PORT/LOG_LEVEL/LOG_FILE (it reads os.environ before the
# app's python-dotenv runs). The web app runs in the foreground; the scheduler,
# MCP server, and tunnel run in the background and are shut down cleanly on Ctrl-C.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# ── Options ────────────────────────────────────────────────────────────────
WITH_TUNNEL=0
WITH_SCHEDULER=1
WITH_MCP=1
for arg in "$@"; do
  case "$arg" in
    --tunnel)   WITH_TUNNEL=1 ;;
    --web-only) WITH_SCHEDULER=0; WITH_MCP=0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

# ── Environment ──────────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  echo "ERROR: .env not found in $ROOT. Copy .env.example to .env first." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a
export FLASK_ENV="${FLASK_ENV:-production}"

# macOS fork() safety: gunicorn forks workers after Obj-C frameworks
# (system proxy / DNS resolution via CFNetwork) are initialized, which
# otherwise aborts the child. See objc[…] +[NSString initialize] fork error.
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

# ── Virtualenv ───────────────────────────────────────────────────────────────
PY="$ROOT/.venv/bin/python"
GUNICORN="$ROOT/.venv/bin/gunicorn"
if [[ ! -x "$PY" || ! -x "$GUNICORN" ]]; then
  echo "ERROR: .venv not found. Create it and install deps:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p logs

# ── Background service cleanup ────────────────────────────────────────────────
PIDS=()
cleanup() {
  echo
  echo "Shutting down services…"
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Scheduler (single-instance; enforces its own scheduler.pid lock) ─────────
if [[ "$WITH_SCHEDULER" == "1" ]]; then
  echo "Starting scheduler → logs/scheduler.log"
  "$PY" -m scheduler.runner >>logs/scheduler.log 2>&1 &
  PIDS+=("$!")
fi

# ── MCP server (Module 11 — external agent access, Streamable HTTP :5010) ────
if [[ "$WITH_MCP" == "1" ]]; then
  echo "Starting MCP server → logs/mcp.log"
  "$PY" -m mcp_server.server >>logs/mcp.log 2>&1 &
  PIDS+=("$!")
fi

# ── Cloudflared tunnel (optional) ────────────────────────────────────────────
if [[ "$WITH_TUNNEL" == "1" ]]; then
  if command -v cloudflared >/dev/null 2>&1; then
    echo "Starting cloudflared tunnel → logs/cloudflared.log"
    cloudflared tunnel --config "$ROOT/deploy/cloudflare/config.yml" run \
      >>logs/cloudflared.log 2>&1 &
    PIDS+=("$!")
  else
    echo "WARNING: --tunnel requested but cloudflared not installed; skipping." >&2
  fi
fi

# ── Web app (foreground) ─────────────────────────────────────────────────────
echo "Starting web app on ${FLASK_HOST:-127.0.0.1}:${FLASK_PORT:-5001} (FLASK_ENV=$FLASK_ENV)"
echo "Press Ctrl-C to stop."
"$GUNICORN" --config "$ROOT/gunicorn.conf.py" wsgi:app
