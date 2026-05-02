#!/usr/bin/env bash
# Start MediaMTX (background) and the FastAPI viewer (foreground).
# Ctrl-C kills both cleanly.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x mediamtx/mediamtx ]]; then
    echo "mediamtx binary not found — run scripts/install-mediamtx.sh" >&2
    exit 1
fi

if [[ ! -f cameras.yaml ]]; then
    echo "cameras.yaml not found — copy cameras.example.yaml and edit" >&2
    exit 1
fi

mediamtx/mediamtx mediamtx/mediamtx.yml &
MTX_PID=$!

cleanup() {
    if kill -0 "$MTX_PID" 2>/dev/null; then
        kill "$MTX_PID"
        wait "$MTX_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

uv run uvicorn dvr.server:app --host 0.0.0.0 --port "${DVR_PORT:-9090}" --no-access-log
