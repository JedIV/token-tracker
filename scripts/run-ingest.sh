#!/usr/bin/env bash
# Wrapper used by launchd. Runs the ingester using uv.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
exec uv run python -m tracker.ingest --db "$HERE/tokens.db"
