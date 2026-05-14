#!/usr/bin/env bash
# Wrapper used by launchd. Runs the ingester using the project venv.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
exec "$HERE/.venv/bin/python" -m tracker.ingest --db "$HERE/tokens.db"
