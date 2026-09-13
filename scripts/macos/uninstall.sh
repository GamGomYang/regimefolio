#!/bin/zsh

set -eu

TARGET_PLIST="$HOME/Library/LaunchAgents/com.inflation-compass.daily.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN" "$TARGET_PLIST" 2>/dev/null || true
echo "Stopped com.inflation-compass.daily; plist retained at $TARGET_PLIST"
