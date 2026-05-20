#!/usr/bin/env bash
# Start the FastAPI server. Use HOST/PORT env vars to override defaults.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8732}"
# --reload watches tracker/ and web/ so editing source rebuilds the running process.
# Set RELOAD=0 to opt out (e.g. when running under launchd).
RELOAD_FLAG="--reload"
[ "${RELOAD:-1}" = "0" ] && RELOAD_FLAG=""
exec uv run python -m uvicorn tracker.api:app --host "$HOST" --port "$PORT" \
    $RELOAD_FLAG --reload-dir tracker --reload-dir web
