#!/usr/bin/env bash
# Start the local paper-mode app (http://localhost:8501). Extra arguments go to the launcher, e.g. --port 8502 or --no-browser.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo "start: no .venv found. Run ./scripts/setup_app.sh first." >&2
  exit 2
fi
exec .venv/bin/python -m src.dashboard.launch "$@"
