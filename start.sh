#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import flask" >/dev/null 2>&1; then
  echo "Installing dependencies..."
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "warning: ffmpeg not found on PATH. Install it with: brew install ffmpeg" >&2
fi

exec python app.py
