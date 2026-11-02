#!/usr/bin/env bash
# Creates the virtual environment on first run, then starts Shimmer Live.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python -m shimmer_live "$@"
