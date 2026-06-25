#!/usr/bin/env bash
set -euo pipefail
VENV_DIR="${VENV_DIR:-.venv}"
python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
