#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
VENV_DIR="${VENV_DIR:-$HOME/venv/$PROJECT_NAME}"
UV_BIN="${UV_BIN:-uv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  echo "uv not found; installing uv with $PYTHON_BIN -m pip --user" >&2
  "$PYTHON_BIN" -m pip install --user --upgrade uv
  export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  echo "ERROR: uv is still not available. Install it from https://docs.astral.sh/uv/ and re-run." >&2
  exit 1
fi

mkdir -p "$(dirname "$VENV_DIR")"
"$UV_BIN" venv --python "$PYTHON_BIN" "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
"$UV_BIN" pip install --upgrade pip
"$UV_BIN" pip install --upgrade -r "$PROJECT_DIR/requirements.txt"

if [ ! -f "$PROJECT_DIR/.env" ] && [ -f "$PROJECT_DIR/.env.example" ]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  echo "Created $PROJECT_DIR/.env from .env.example; edit tokens and upstream settings before production use." >&2
fi

echo "Installed/updated $PROJECT_NAME in $VENV_DIR"
echo "Start with: source $PROJECT_DIR/run.sh [IP] [PORT]"
