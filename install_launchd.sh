#!/bin/zsh

set -eu

PROJECT_DIR="/Users/gamgomyang/vscode/regimefolio"
SOURCE_PLIST="$PROJECT_DIR/launchd/com.inflation-compass.daily.plist"
TARGET_PLIST="/Users/gamgomyang/Library/LaunchAgents/com.inflation-compass.daily.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/data/backups" "/Users/gamgomyang/Library/LaunchAgents"
chmod +x "$PROJECT_DIR/launch_daily.sh"
plutil -lint "$SOURCE_PLIST"
cp "$SOURCE_PLIST" "$TARGET_PLIST"
launchctl bootout "$DOMAIN" "$TARGET_PLIST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
launchctl kickstart -k "$DOMAIN/com.inflation-compass.daily"
echo "Installed and started com.inflation-compass.daily"
echo "Status: launchctl print $DOMAIN/com.inflation-compass.daily"
