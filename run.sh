#!/usr/bin/env bash
# Start the privacy-filter proxy. Can be executed by systemd or sourced by a shell:
#   ./run.sh 0.0.0.0 8088
#   source ./run.sh 127.0.0.1 8088

_privacy_proxy_sourced=0
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  _privacy_proxy_sourced=1
fi

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
HOST="${1:-${HOST:-0.0.0.0}}"
PORT="${2:-${PORT:-8088}}"
VENV_DIR="${VENV_DIR:-$HOME/venv/$PROJECT_NAME}"

if [ -f "$PROJECT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

export HOST="${1:-${HOST:-0.0.0.0}}"
export PORT="${2:-${PORT:-8088}}"

if [ -f "$VENV_DIR/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
elif [ -f "$PROJECT_DIR/.venv/bin/activate" ]; then
  # Backward-compatible fallback for older local installs.
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.venv/bin/activate"
else
  echo "Virtual environment not found at $VENV_DIR. Run ./install.sh first." >&2
  if [ "$_privacy_proxy_sourced" -eq 1 ]; then
    return 1
  fi
  exit 1
fi

if [ "$_privacy_proxy_sourced" -eq 1 ]; then
  uvicorn app:app --host "$HOST" --port "$PORT" --log-level "${LOG_LEVEL:-info}"
else
  exec uvicorn app:app --host "$HOST" --port "$PORT" --log-level "${LOG_LEVEL:-info}"
fi
