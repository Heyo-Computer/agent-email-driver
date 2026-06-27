#!/usr/bin/env bash
# Launch the factory daemon. Loads .env (if present) and runs factoryd.py.
# Usage: ./run.sh [--once|--probe|--verbose]
set -euo pipefail

cd "$(dirname "$0")"

# Prefer uv if available (handles any future deps); fall back to python3.
if command -v uv >/dev/null 2>&1; then
  exec uv run --python 3.12 python factoryd.py "$@"
else
  exec python3 factoryd.py "$@"
fi
