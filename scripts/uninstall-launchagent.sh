#!/usr/bin/env bash
set -euo pipefail
TARGET="$HOME/Library/LaunchAgents/com.user.token-tracker.plist"
launchctl unload -w "$TARGET" 2>/dev/null || true
rm -f "$TARGET"
echo "Removed: $TARGET"
