#!/bin/zsh

set -eu

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SOURCE_PLIST="$PROJECT_DIR/scripts/macos/com.inflation-compass.daily.plist"
TARGET_PLIST="$HOME/Library/LaunchAgents/com.inflation-compass.daily.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/data/backups" "$HOME/Library/LaunchAgents"
chmod +x "$PROJECT_DIR/scripts/macos/run_daily.sh"
"$PROJECT_DIR/.venv/bin/python" - "$SOURCE_PLIST" "$TARGET_PLIST" "$PROJECT_DIR" <<'PYTHON'
import plistlib
import sys
from pathlib import Path
source, target, project = map(Path, sys.argv[1:])
with source.open("rb") as stream:
    config = plistlib.load(stream)
config["ProgramArguments"] = [str(project / "scripts/macos/run_daily.sh")]
config["StandardOutPath"] = str(project / "logs/daily_job.log")
config["StandardErrorPath"] = str(project / "logs/daily_job_error.log")
with target.open("wb") as stream:
    plistlib.dump(config, stream)
PYTHON
plutil -lint "$TARGET_PLIST"
launchctl bootout "$DOMAIN" "$TARGET_PLIST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
launchctl kickstart -k "$DOMAIN/com.inflation-compass.daily"
echo "Installed and started com.inflation-compass.daily"
echo "Status: launchctl print $DOMAIN/com.inflation-compass.daily"
