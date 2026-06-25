#!/usr/bin/env bash
set -euo pipefail
HOST="${1:-${HOST:-0.0.0.0}}"
PORT="${2:-${PORT:-8088}}"
export HOST PORT
VENV_DIR="${VENV_DIR:-.venv}"
if [ -f "$VENV_DIR/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
fi
exec uvicorn app:app --host "$HOST" --port "$PORT" --log-level "${LOG_LEVEL:-info}"
