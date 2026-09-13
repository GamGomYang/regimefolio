#!/bin/zsh

set -eu

PROJECT_DIR="/Users/gamgomyang/vscode/regimefolio"
LOCK_DIR="$PROJECT_DIR/data/.daily_job.lock"

mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/data/backups"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

cd "$PROJECT_DIR"
"$PROJECT_DIR/.venv/bin/python" daily_job.py
