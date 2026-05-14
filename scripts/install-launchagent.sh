#!/usr/bin/env bash
# Install + load the launchd agent that runs ingest every 5 minutes.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$HOME/Library/LaunchAgents/com.user.token-tracker.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__HOME__|$HOME|g; s|__PROJECT__|$HERE|g" "$HERE/scripts/com.user.token-tracker.plist" > "$TARGET"

# Replace any existing instance
launchctl unload -w "$TARGET" 2>/dev/null || true
launchctl load -w "$TARGET"

echo "Installed: $TARGET"
echo "Status:   launchctl list | grep com.user.token-tracker"
echo "Logs:     tail -f \"$HOME/Library/Logs/token-tracker.log\""
