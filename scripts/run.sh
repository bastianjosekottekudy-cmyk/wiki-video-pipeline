#!/usr/bin/env bash
# Start the Wiki Video Pipeline dashboard and scheduler on Linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

if [ ! -f "$REPO_ROOT/.venv/bin/python" ]; then
    echo "Virtual environment not found. Initializing..."
    python3 -m venv "$REPO_ROOT/.venv"
    "$REPO_ROOT/.venv/bin/pip" install -r "$REPO_ROOT/requirements.txt"
fi

SYSTEM_FFMPEG="$(which ffmpeg 2>/dev/null || true)"
if [ -n "$SYSTEM_FFMPEG" ] && [ -z "${IMAGEIO_FFMPEG_EXE:-}" ]; then
    export IMAGEIO_FFMPEG_EXE="$SYSTEM_FFMPEG"
fi

echo "Starting Wiki Video Pipeline..."
echo "Dashboard: http://127.0.0.1:8082"
exec "$REPO_ROOT/.venv/bin/python" -m src.main
