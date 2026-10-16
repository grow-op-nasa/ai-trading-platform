#!/usr/bin/env bash
# One-time (and safe-to-repeat) local setup. It installs declared dependencies into ./.venv and creates empty directories.
# It never touches data/, never creates a portfolio or database, never runs a backtest, never sends email, never calls an AI.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
"$PY" - <<'PYCHK'
import sys
if sys.version_info < (3, 12):
    sys.exit("setup: Python 3.12+ is required (found %d.%d). Install it, then run: PYTHON=python3.12 scripts/setup_app.sh" % sys.version_info[:2])
PYCHK
if [ ! -x .venv/bin/python ]; then
  echo "setup: creating .venv"
  "$PY" -m venv .venv
fi
echo "setup: installing requirements (pinned in requirements.txt)"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
mkdir -p data logs
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "setup: created .env from .env.example (all settings commented out; nothing is enabled)"
else
  echo "setup: kept your existing .env"
fi
.venv/bin/python -m src.dashboard.launch --check
echo "setup: done. Start the app with: ./scripts/start_app.sh"
