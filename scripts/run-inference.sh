#!/usr/bin/env bash
# Entry point for belfry-inference.service. Runs the multi-camera
# inference runner inside the dedicated Python 3.10 venv that
# scripts/install-inference.sh provisions.

set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -x .venv-inference/bin/python ]]; then
    echo ".venv-inference/bin/python missing — run scripts/install-inference.sh" >&2
    exit 1
fi

exec .venv-inference/bin/python -m inference.runner
